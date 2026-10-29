#!/usr/bin/env python3
"""code-health guard: a ratchet, not a linter.

Vendored into the target repository at .code-health/guard.py and run by
pre-commit. Self-contained on purpose: a colleague without the plugin and a CI
runner must both pass this hook, so it never imports code_health.

ONE RULE:
    no entity may exceed max(its baseline value, warn)

A file or function absent from the baseline has no baseline value, so its
ceiling is warn. There is no separate, more permissive case for new code --
that would let a brand-new 800-line file through while failing an existing
401-line one.

In the hook path this makes no git calls: pre-commit passes the file list and
stashes unstaged changes, so reading from disk reads the staged content.
"""

import argparse
import ast
import csv
import datetime
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tokenize
from pathlib import Path

BASELINE_RELPATH = Path(".code-health") / "baseline.json"
CONFIG_RELPATH = Path(".code-health") / "config.json"

DEFAULT_THRESHOLDS = {
    "file_lines": {"warn": 400, "hard": 800},
    "file_tokens": {"warn": 8000, "hard": 15000},
    "function_lines": {"warn": 60, "hard": 100},
    "function_ccn": {"warn": 15, "hard": 25},
    "parameters": {"warn": 5, "hard": 8},
    "nesting": {"warn": 3, "hard": 4},
}
DEFAULT_CHARS_PER_TOKEN = 4

# "This file is code." A verbatim copy of code_health/discovery.py's
# SOURCE_EXT -- duplicated for the same reason the CSV parser below is, and
# pinned to that copy by tests/test_parser_agreement.py. The audit and the
# ratchet disagreeing about what a file is was a real defect: the audit used
# to measure Markdown as source while this hook never has.
SUPPORTED_EXT = {
    ".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".java",
    ".kt", ".rs", ".rb", ".php", ".c", ".h", ".cpp", ".cc", ".hpp",
    ".cs", ".swift", ".scala", ".lua", ".m", ".mm",
}

FUNC_FIELDS = ("nloc", "ccn", "token", "param", "length", "location",
               "filename", "function_name", "long_name", "start_line",
               "end_line", "nesting")
_INT_FIELDS = {"nloc", "ccn", "token", "param", "length", "start_line",
               "end_line", "nesting"}

# Extensions whose lizard reader was verified, with a flat-vs-deep probe, to
# reset its nesting counter correctly (see code_health/sizes.py and
# references/tools.md for the full verification). Every other extension,
# `.py` included, never keeps lizard's raw nesting number.
_LIZARD_NESTING_OK_EXT = {
    ".c", ".h", ".cpp", ".cc", ".hpp",
    ".cs", ".java",
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
    ".go", ".kt", ".rs", ".swift", ".scala", ".php",
    ".m", ".mm",
}

# Same nesting-depth pass as code_health/pyast.py, duplicated on purpose --
# this file never imports code_health, the same reason the CSV parser above
# is duplicated rather than shared. Keep the two in sync; the parser
# agreement test (tests/test_parser_agreement.py) checks they still agree.
_NESTING_STMTS = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try)
if hasattr(ast, "TryStar"):     # 3.11+, `except*`
    _NESTING_STMTS = _NESTING_STMTS + (ast.TryStar,)
if hasattr(ast, "Match"):       # 3.10+, `match`/`case`
    _NESTING_STMTS = _NESTING_STMTS + (ast.Match,)


def _if_depth(node, depth):
    """One `if`/`elif`/`else` chain is one nesting level, not one per branch
    -- `elif` has no AST node of its own (see code_health/pyast.py)."""
    deepest = _max_depth(node.body, depth + 1)
    orelse = node.orelse
    if len(orelse) == 1 and isinstance(orelse[0], ast.If):
        deepest = max(deepest, _if_depth(orelse[0], depth))
    elif orelse:
        deepest = max(deepest, _max_depth(orelse, depth + 1))
    return deepest


def _stmt_bodies(node):
    """Every statement list one nesting construct can hold, each at the
    SAME depth as its siblings (an `except`/`else`/`finally` clause is not
    additional beyond its `try`; one `case` is not additional beyond its
    `match`)."""
    if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
        bodies = [node.body]
        if node.orelse:
            bodies.append(node.orelse)
        return bodies
    if isinstance(node, ast.Try) or (hasattr(ast, "TryStar") and isinstance(node, ast.TryStar)):
        bodies = [node.body] + [handler.body for handler in node.handlers]
        if node.orelse:
            bodies.append(node.orelse)
        if node.finalbody:
            bodies.append(node.finalbody)
        return bodies
    if hasattr(ast, "Match") and isinstance(node, ast.Match):
        return [case.body for case in node.cases]
    return [node.body]     # With / AsyncWith


def _max_depth(stmts, depth):
    """Deepest nesting depth inside `stmts`, starting at `depth`. Does not
    descend into a nested function/class -- each gets its own top-level
    call in `_python_nesting`, starting back at depth 0."""
    deepest = depth
    for stmt in stmts:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(stmt, ast.If):
            deepest = max(deepest, _if_depth(stmt, depth))
        elif isinstance(stmt, _NESTING_STMTS):
            for body in _stmt_bodies(stmt):
                deepest = max(deepest, _max_depth(body, depth + 1))
    return deepest


def max_nesting(node):
    """A function's own maximum nesting depth; see code_health/pyast.py for
    the full rationale -- this is the same algorithm."""
    return _max_depth(node.body, 0)


def _python_nesting(source):
    """`{start_line: nesting}` for every function `ast.walk` finds in
    `source`. Empty on a syntax error: guard never fails a commit just
    because a file it is checking cannot be parsed here either -- any
    lizard row for it simply comes back unmatched, i.e. `nesting = None`,
    same as any other row this map does not cover."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return {}
    return {
        node.lineno: max_nesting(node)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


# Tracker keys in comments. The same reader and detector as
# code_health/comments.py (`ticket_refs`, `extract_brace`, `_physical_lines`),
# duplicated for the reason the nesting pass above is; the parser agreement
# test checks the two count the same lines.
_TICKET_KEY_RE = re.compile(r"(?<!-)\b([A-Z][A-Z0-9]+)-\d+\b")
_ISSUE_NUMBER_RE = re.compile(
    r"\b(?:issues?|pr|pull|bug|fix(?:es|ed)?|close[sd]?|resolve[sd]?|see"
    r"|задач[аеиуой]?|тикет[аеуом]?)\s*#\d+\b", re.IGNORECASE)
_URL_RE = re.compile(r"https?://\S+")
_TASK_TAG_RE = re.compile(r"\b(?:TODO|FIXME|XXX|HACK)\b")
NOT_A_TRACKER = frozenset({
    "UTF", "UCS", "ISO", "IEC", "IEEE", "ANSI", "ECMA", "ES", "RFC", "PEP",
    "CVE", "CWE", "SHA", "MD", "AES", "RSA", "HMAC", "CRC", "KOI", "CP", "WIN",
    "WINDOWS", "LATIN", "ASCII", "BASE", "HTTP", "TLS", "SSL", "IPV", "HTML",
    "CSS", "JSON", "PKCS", "Q1", "Q2", "Q3", "Q4", "COVID", "GPT", "X86",
    "SHA3", "MPEG", "RS", "DDR", "SIZE", "LEN", "LENGTH", "MAX", "MIN", "COUNT",
    "IDX", "NUM",
})
BRACE_EXT = {
    ".c", ".h", ".cpp", ".cc", ".hpp", ".cs", ".java",
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
    ".go", ".kt", ".rs", ".swift", ".scala",
}
# A key in a new file fails; an old file may keep the keys it had.
TICKET_REF_WARN = 1


def ticket_refs(line, ignore=frozenset()):
    if _TASK_TAG_RE.search(line):
        return []
    line = _URL_RE.sub(" ", line)
    keys = [m.group(0) for m in _TICKET_KEY_RE.finditer(line)
            if m.group(1) not in NOT_A_TRACKER and m.group(1) not in ignore]
    return keys + [m.group(0) for m in _ISSUE_NUMBER_RE.finditer(line)]


def _python_comment_lines(text, src_lines):
    """`(line, text)` per comment line, or None when the source does not parse."""
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
        tree = ast.parse(text)
    except (tokenize.TokenError, SyntaxError, ValueError):
        return None
    out = []
    for tok in toks:
        if tok.type != tokenize.COMMENT:
            continue
        row, col = tok.start
        before = src_lines[row - 1][:col] if row - 1 < len(src_lines) else ""
        out.append((row, tok.string if before.strip() else src_lines[row - 1]))
    for node in ast.walk(tree):
        if (isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                              ast.AsyncFunctionDef))
                and ast.get_docstring(node) is not None):
            expr = node.body[0]
            out.extend((n, src_lines[n - 1])
                       for n in range(expr.lineno, expr.end_lineno + 1))
    return out


def _brace_comment_lines(text, src_lines):
    out = []
    i, n, line = 0, len(text), 1
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch in "\"'`":
            i, line = _skip_literal(text, i, line)
            continue
        if ch == "/" and nxt in "/*":
            if nxt == "/":
                end = text.find("\n", i + 2)
                end = n if end == -1 else end
                body, stop = text[i + 2:end], end
            else:
                end = text.find("*/", i + 2)
                body = text[i + 2:end if end != -1 else n]
                stop = n if end == -1 else end + 2
            last = line + body.count("\n")
            if _has_code_before(text, i) and last == line:
                out.append((line, body))
            else:
                out.extend((k, src_lines[k - 1])
                           for k in range(line, min(last, len(src_lines)) + 1))
            line, i = last, stop
            continue
        if ch == "\n":
            line += 1
        i += 1
    return out


def _skip_literal(text, i, line):
    quote, n = text[i], len(text)
    i += 1
    while i < n:
        ch = text[i]
        if ch == "\\":
            if text[i + 1:i + 2] == "\n":
                line += 1
            i += 2
            continue
        if ch == "\n":
            line += 1
        if ch == quote:
            return i + 1, line
        i += 1
    return n, line


def _has_code_before(text, i):
    start = text.rfind("\n", 0, i) + 1
    return bool(text[start:i].strip())


def count_ticket_refs(text, ext, ignore=frozenset()):
    """Comment lines carrying a tracker key, or None when no reader covers `ext`."""
    src_lines = text.split("\n")
    if ext == ".py":
        found = _python_comment_lines(text, src_lines)
    elif ext in BRACE_EXT:
        found = _brace_comment_lines(text, src_lines)
    else:
        return None
    if found is None:
        return None
    return len({n for n, line in found if ticket_refs(line, ignore)})


def parse_lizard_csv(text):
    rows = []
    for raw in csv.reader(io.StringIO(text)):
        if not raw:
            continue
        if len(raw) != len(FUNC_FIELDS):
            raise SystemExit("code-health guard: unexpected lizard output "
                             "(%d columns, expected %d). Is -ENS set?"
                             % (len(raw), len(FUNC_FIELDS)))
        row = dict(zip(FUNC_FIELDS, raw))
        for field in _INT_FIELDS:
            row[field] = int(row[field])
        rows.append(row)
    return rows


def exceeds(value, ceiling):
    """The one comparison. Fires at value >= ceiling, never at >."""
    return value >= ceiling


def resolve_lizard():
    override = os.environ.get("CODE_HEALTH_LIZARD")
    if override:
        return [override]
    found = shutil.which("lizard")
    if found:
        return [found]
    probe = subprocess.run([sys.executable, "-m", "lizard", "--version"],
                           capture_output=True)
    return [sys.executable, "-m", "lizard"] if probe.returncode == 0 else None


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


# Verbatim copy of code_health/discovery.py's gitwildmatch matcher, for the
# same reason as the CSV parser and the nesting pass: this file never imports
# code_health. The previous `fnmatch` shortcut disagreed with the measurer on
# six of thirteen plausible patterns, in BOTH directions -- so a repository's
# own `exclude` list meant one thing to the audit and another to the ratchet.
# `references/tools.md` states that not using fnmatch here is a standing rule.
# tests/test_parser_agreement.py pins the two copies to each other.
def compile_pattern(pattern):
    """Translate one gitwildmatch pattern into a regex over a POSIX relpath."""
    anchored = pattern.startswith("/")
    pat = (pattern[1:] if anchored else pattern).rstrip("/")
    # A pattern with no slash matches its basename at any depth.
    if "/" not in pat:
        anchored = False
        pat = "**/" + pat

    parts = pat.split("/")
    out = ["^"]
    if not anchored and parts and parts[0] != "**":
        out.append("(?:[^/]+/)*")

    for index, segment in enumerate(parts):
        last = index == len(parts) - 1
        if segment == "**":
            # `**/` is zero or more directories; a trailing `/**` is everything below.
            out.append(".*" if last else "(?:[^/]+/)*")
            continue
        out.append(_segment_regex(segment))
        if not last:
            out.append("/")
    # A matched directory takes everything below it with it: a bare `vendor`
    # or `build/` copied out of .gitignore must exclude what is inside it.
    out.append("(?:/.*)?$")
    return re.compile("".join(out))


def _segment_regex(segment):
    buf = []
    for ch in segment:
        if ch == "*":
            buf.append("[^/]*")
        elif ch == "?":
            buf.append("[^/]")
        else:
            buf.append(re.escape(ch))
    return "".join(buf)


def compile_excludes(patterns):
    """Compile once per run, not once per path: the baseline pass walks every
    tracked file in the repository."""
    return [compile_pattern(p) for p in patterns]


def excluded(relpath, compiled):
    return any(rx.match(relpath) for rx in compiled)


def settings_for(repo_root):
    cfg = load_json(Path(repo_root) / CONFIG_RELPATH, {})
    merged = {k: dict(v) for k, v in DEFAULT_THRESHOLDS.items()}
    for name, override in (cfg.get("thresholds") or {}).items():
        if name in merged:
            merged[name].update(override)
    return (merged,
            _chars_per_token(cfg.get("chars_per_token", DEFAULT_CHARS_PER_TOKEN)),
            compile_excludes(cfg.get("exclude", [])),
            _ticket_ignore(cfg.get("ticket_ref_ignore")))


def _ticket_ignore(value):
    # The audit refuses a malformed list; the guard must not block a commit on
    # it, so it ignores the setting rather than read "TIER" as four letters.
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return frozenset(value)
    return frozenset()


def _chars_per_token(value):
    """It is a divisor. `0` in a repository's config used to reach the token
    estimate and abort the commit with a ZeroDivisionError traceback, where
    the measurer's own `config.estimate_tokens` raises a clean error."""
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise SystemExit(
            "code-health guard: chars_per_token must be a positive number, "
            "got %r in %s" % (value, CONFIG_RELPATH))
    return value


def _read_files(repo_root, relpaths, chars_per_token, ticket_ignore=frozenset()):
    """File-level measurements plus the Python nesting map, from disk only."""
    files, python_nesting = {}, {}
    for rel in relpaths:
        try:
            text = (repo_root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        files[rel] = {
            "lines": len(text.splitlines()),
            "tokens": int(math.ceil(len(text) / float(chars_per_token))),
            "ticket_refs": count_ticket_refs(text, Path(rel).suffix, ticket_ignore),
            "functions": {},
        }
        if rel.endswith(".py"):
            python_nesting[rel] = _python_nesting(text)
    return files, python_nesting


# Chunked so a large repository does not exceed the OS argv limit -- same
# reason and same size as code_health/sizes.py. `--baseline` passes every
# tracked source file in one call: measured, 17,118 paths raises an uncaught
# `OSError: Argument list too long`, i.e. a traceback out of
# `guard install` inside someone else's repository.
_ARG_CHUNK = 400


def _lizard_rows(repo_root, argv, relpaths):
    rows = []
    for start in range(0, len(relpaths), _ARG_CHUNK):
        proc = subprocess.run(
            argv + ["-ENS", "--csv"] + list(relpaths[start:start + _ARG_CHUNK]),
            cwd=str(repo_root), capture_output=True, text=True)
        rows.extend(parse_lizard_csv(proc.stdout))
    return rows


def _nesting_for(row, python_nesting):
    ext = Path(row["filename"]).suffix
    if ext == ".py":
        # Lizard's own Python nesting column is a monotonically increasing
        # counter across the whole invocation (verified in sizes.py /
        # references/tools.md), never trusted here. A row whose start line
        # has no match in `_python_nesting` (should be rare; see
        # pyast.measure_nesting) is `None`, not a guess.
        return python_nesting.get(row["filename"], {}).get(row["start_line"])
    if ext in _LIZARD_NESTING_OK_EXT:
        return row["nesting"]
    return None


def function_key(existing, name):
    """The baseline key for one function row: its name, disambiguated by
    order of appearance in the file when the name is not unique there.

    lizard reports BARE names for Python methods -- `A.handler` and
    `B.handler` both arrive as `handler` (verified: two rows, one
    `function_name`). Keying the baseline on the bare name collapsed them
    into one entry, last row winning, so a 201-line method and a 2-line
    method in one file recorded `{"handler": {"lines": 2}}` and the
    201-line one vanished from the ratchet permanently -- appending a tiny
    same-named function was enough to hide an arbitrarily large one.

    The key is deliberately NOT `(name, start_line)` or lizard's `location`
    field, both of which carry line numbers. Demonstrated: adding one
    `import` line at the top of a file moves a 200-line grandfathered
    function from line 1 to line 2, which under a line-bearing key reads as
    a NEW entity and drops its ceiling from 200 to `function_lines.warn`
    (60) -- failing a commit that never touched it. Order of appearance
    survives every edit above the function and still separates same-named
    siblings; only reordering them reads as new, which is the same
    behaviour a rename already has.
    """
    if name not in existing:
        return name
    index = 2
    while "%s#%d" % (name, index) in existing:
        index += 1
    return "%s#%d" % (name, index)


def measure(repo_root, relpaths, chars_per_token, ticket_ignore=frozenset()):
    repo_root = Path(repo_root)
    files, python_nesting = _read_files(repo_root, relpaths, chars_per_token,
                                        ticket_ignore)
    argv = resolve_lizard()
    if argv is None or not files:
        return files, argv is not None
    rows = _lizard_rows(repo_root, argv, list(files))
    # lizard does not emit rows in source order (a nested function precedes
    # its parent), and `function_key` numbers duplicates by appearance, so
    # the ordering has to be imposed here or the key would depend on
    # lizard's internal traversal.
    for row in sorted(rows, key=lambda r: (r["filename"], r["start_line"])):
        entry = files.get(row["filename"])
        if entry is None:
            continue
        entry["functions"][function_key(entry["functions"], row["function_name"])] = {
            "lines": row["length"], "ccn": row["ccn"],
            "params": row["param"], "nesting": _nesting_for(row, python_nesting),
        }
    return files, True


# (label, measured key, threshold key). The labels are spelled out because
# this file is vendored into repositories whose owners have never heard of
# this plugin or of lizard, and the failure report is its only user-facing
# surface: `ccn` and `nesting` mean nothing to someone reading a rejected
# commit for the first time.
_FILE_CHECKS = (("file lines", "lines", "file_lines"),
                ("estimated tokens", "tokens", "file_tokens"),
                ("tracker keys in comments", "ticket_refs", "ticket_refs"))
# Checks whose warn is fixed here, not read from `config.thresholds`.
_FIXED_WARN = {"ticket_refs": TICKET_REF_WARN}
_FUNC_CHECKS = (("function lines", "lines", "function_lines"),
                ("cyclomatic complexity", "ccn", "function_ccn"),
                ("parameters", "params", "parameters"),
                ("nesting depth", "nesting", "nesting"))


def ceiling(baseline_value, warn):
    """The value at which this entity starts failing, compared with `exceeds`.

    A baseline is a value already granted, so it stays legal: an existing
    900-line file must pass at 900 and fail at 901, hence `baseline + 1`.
    A threshold is a line not to cross, so `warn` is used as-is: `level_for`
    fires warn at exactly 400, and so must this.
    """
    if baseline_value is None:
        return warn
    return max(baseline_value + 1, warn)


def compare(measured, baseline, thresholds):
    violations = []
    base_files = baseline.get("files", {})
    for path, current in sorted(measured.items()):
        base = base_files.get(path, {})
        base_funcs = base.get("functions", {})
        if path in base_files and base.get("ticket_refs") is None:
            # A baseline written before this metric existed, or on a Python
            # that could not parse the file, grants nothing and forbids
            # nothing for it until `update-baseline`.
            current = dict(current, ticket_refs=None)
        entities = [(None, current, base, _FILE_CHECKS)]
        entities.extend((name, func, base_funcs.get(name, {}), _FUNC_CHECKS)
                        for name, func in sorted(current["functions"].items()))
        for entity, values, base_values, checks in entities:
            violations.extend(
                _entity_violations(path, entity, values, base_values, checks, thresholds))
    return violations


def _entity_violations(path, entity, values, base_values, checks, thresholds):
    """Every violation for one entity (a file, or one function in it)
    against the checks that apply to it -- a violation or nothing per
    check, same ceiling rule (`ceiling`/`exceeds`) either way."""
    out = []
    for label, key, tkey in checks:
        value = values.get(key)
        # `nesting` is `None` for a language never verified safe to trust
        # lizard's nesting reader for (or an unmatched Python row) -- not
        # checked at all, never checked against a wrong number. `ticket_refs`
        # is `None` for a language with no comment reader, a Python file that
        # does not parse, or a file whose baseline predates the metric.
        if value is None:
            continue
        warn = _FIXED_WARN[tkey] if tkey in _FIXED_WARN else thresholds[tkey]["warn"]
        limit = ceiling(base_values.get(key), warn)
        if exceeds(value, limit):
            out.append({
                "path": path, "entity": entity, "metric": label,
                "value": value, "limit": limit,
                "baseline": base_values.get(key), "warn": warn,
            })
    return out


def report(violations):
    print("code-health guard: %d violation(s)\n" % len(violations))
    for v in violations:
        where = "%s::%s" % (v["path"], v["entity"]) if v["entity"] else v["path"]
        origin = ("baseline %s" % v["baseline"]) if v["baseline"] is not None \
            else ("new, threshold %s" % v["warn"])
        # `>=`, not `>`: `exceeds` fires at value >= limit, so a real
        # violation used to print as `lines 400 > 400`.
        print("  %-44s %-22s %s >= %s (%s)"
              % (where, v["metric"], v["value"], v["limit"], origin))
    print("\nOld debt is grandfathered; this is new growth.")
    print("To move the ratchet deliberately:  /code-health:guard update-baseline")
    print("To bypass this one commit:         git commit --no-verify")


def threshold_drift(frozen, current):
    """Metrics whose threshold moved since the baseline was frozen.

    `baseline["thresholds"]` was written and never read. DESIGN 10 says the
    frozen copy exists so drift can be REPORTED: lowering a threshold
    tightens the ceiling immediately, which is the intended way to raise the
    bar, and raising one is a loosening that must be announced rather than
    take effect quietly.
    """
    moved = []
    for name, levels in sorted(current.items()):
        was = (frozen or {}).get(name) or {}
        for level in ("warn", "hard"):
            before, now = was.get(level), levels.get(level)
            if before is None or now is None or before == now:
                continue
            moved.append({"metric": name, "level": level, "from": before,
                          "to": now,
                          "direction": "loosened" if now > before else "tightened"})
    return moved


def report_drift(moved):
    if not moved:
        return
    print("code-health guard: thresholds moved since the baseline was frozen\n")
    for row in moved:
        print("  %-9s %-16s %-5s %s -> %s"
              % (row["direction"], row["metric"], row["level"],
                 row["from"], row["to"]))
    if any(row["direction"] == "loosened" for row in moved):
        print("\n  A loosened threshold raises the ceiling for every entity that has no\n"
              "  baseline value of its own. That is the bar moving down, not code improving.")
    print("")


# Which numbers a baseline diff compares, per entity kind.
_FILE_MOVES = (("file lines", "lines"), ("estimated tokens", "tokens"),
               ("tracker keys in comments", "ticket_refs"))
_FUNC_MOVES = (("function lines", "lines"), ("cyclomatic complexity", "ccn"),
               ("parameters", "params"), ("nesting depth", "nesting"))
_MAX_LISTED_MOVES = 20


def _entity_pairs(previous, current):
    """`(path, entity, metrics, before, after)` for every file and function
    present in BOTH baselines. Anything only in one of them is a new or
    removed entity, not a movement, and is counted separately."""
    for path, after in sorted(current.items()):
        before = previous.get(path)
        if before is None:
            continue
        yield path, None, _FILE_MOVES, before, after
        for name, func_after in sorted(after.get("functions", {}).items()):
            func_before = (before.get("functions") or {}).get(name)
            if func_before is not None:
                yield path, name, _FUNC_MOVES, func_before, func_after


def baseline_moves(previous, current):
    """`(worse, better)` -- every metric that moved, in each direction."""
    worse, better = [], []
    for path, entity, metrics, before, after in _entity_pairs(previous or {}, current):
        for label, key in metrics:
            was, now = before.get(key), after.get(key)
            if was is None or now is None or now == was:
                continue
            (worse if now > was else better).append(
                {"path": path, "entity": entity, "metric": label,
                 "from": was, "to": now})
    return worse, better


def report_baseline_moves(previous, current, moves):
    """What moved in each direction, with every upward move named.

    `--baseline` used to overwrite the file before anything could diff it,
    so "prints what moved in each direction, calling out explicitly any
    metric that moved up" (DESIGN 10, guard SKILL 5) was unimplementable.
    The upward callout is the only thing that makes a slipping ratchet
    visible: an entity's new value becomes its permanent floor.
    """
    worse, better = moves
    previous = previous or {}
    print("  files: %d measured, %d new, %d gone"
          % (len(current), len(set(current) - set(previous)),
             len(set(previous) - set(current))))
    print("  metrics on entities present in both: %d improved, %d worsened"
          % (len(better), len(worse)))
    for row in worse[:_MAX_LISTED_MOVES]:
        where = "%s::%s" % (row["path"], row["entity"]) if row["entity"] else row["path"]
        print("  UP  %-44s %-22s %s -> %s"
              % (where, row["metric"], row["from"], row["to"]))
    if len(worse) > _MAX_LISTED_MOVES:
        print("  UP  ... and %d more" % (len(worse) - _MAX_LISTED_MOVES))
    if worse:
        print("\n  Every UP line is the ratchet moving the wrong way: each of those values is\n"
              "  now the permanent floor for that entity. Move the baseline after a real\n"
              "  improvement, never to get past a failing commit.")


def write_baseline(repo_root, target, settings):
    thresholds, chars_per_token, exclude, ticket_ignore = settings
    files, had_lizard = measure(repo_root, _all_supported(repo_root, exclude),
                                chars_per_token, ticket_ignore)
    if not had_lizard:
        sys.stderr.write("code-health guard: lizard not available\n")
        return 2
    target = Path(target)
    # Read the OUTGOING baseline before overwriting it -- that diff is the
    # whole point of `update-baseline` printing what moved.
    previous = load_json(target, {}).get("files") or {}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "schema": 1,
        "generated_at": _now(),
        "head": _head(repo_root),
        "thresholds": thresholds,
        "files": files,
    }, indent=2, sort_keys=True), encoding="utf-8")
    print("code-health guard: baseline written for %d file(s)" % len(files))
    report_baseline_moves(previous, files, baseline_moves(previous, files))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="guard.py")
    parser.add_argument("files", nargs="*")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--baseline")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo).resolve()
    settings = settings_for(repo_root)
    thresholds, chars_per_token, exclude, ticket_ignore = settings

    if args.baseline:
        return write_baseline(repo_root, args.baseline, settings)

    targets = _changed(repo_root) if args.check else list(args.files)
    targets = [t for t in targets
               if Path(t).suffix in SUPPORTED_EXT and not excluded(t, exclude)]
    # The hook path exits before touching disk when a commit carries nothing
    # this tool measures; `check` still has a threshold drift to announce.
    if not targets and not args.check:
        return 0

    baseline_path = repo_root / BASELINE_RELPATH
    if not baseline_path.exists():
        print("code-health guard: no baseline at %s; nothing to ratchet against. "
              "Run /code-health:guard install." % BASELINE_RELPATH)
        return 0

    baseline = load_json(baseline_path, {"files": {}})
    drift = threshold_drift(baseline.get("thresholds"), thresholds)
    if args.check:
        report_drift(drift)
    if not targets:
        return 0
    measured, had_lizard = measure(repo_root, targets, chars_per_token, ticket_ignore)
    if not had_lizard:
        print("code-health guard: lizard unavailable; file sizes checked only")
    violations = compare(measured, baseline, thresholds)
    if not violations:
        return 0
    if not args.check:
        # In the hook path drift is printed only alongside a failure, where
        # it explains the ceiling; `check` announces it every time.
        report_drift(drift)
    report(violations)
    return 1


def _now():
    """UTC, second resolution, `Z` suffix -- the same shape measure.py's
    `generated_at` uses. DESIGN 10's baseline schema shows the field; it
    was the one key in that schema the writer never produced."""
    return (datetime.datetime.now(datetime.timezone.utc)
            .replace(microsecond=0).isoformat().replace("+00:00", "Z"))


def _head(repo_root):
    try:
        out = subprocess.run(["git", "-C", str(repo_root), "rev-parse", "HEAD"],
                             capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _all_supported(repo_root, exclude):
    out = subprocess.run(["git", "-C", str(repo_root), "ls-files", "-z"],
                         capture_output=True)
    if out.returncode == 0:
        names = [p for p in out.stdout.decode("utf-8", "replace").split("\0") if p]
    else:
        names = [str(p.relative_to(repo_root))
                 for p in Path(repo_root).rglob("*") if p.is_file()]
    return [n for n in names
            if Path(n).suffix in SUPPORTED_EXT and not excluded(n, exclude)]


def _changed(repo_root):
    """Manual-check mode only. `git diff` does not list untracked files, and
    untracked files are exactly the new-entity case the ratchet exists for."""
    names = []
    for argv in (["diff", "--name-only", "HEAD"],
                 ["ls-files", "--others", "--exclude-standard"]):
        out = subprocess.run(["git", "-C", str(repo_root)] + argv,
                             capture_output=True, text=True)
        if out.returncode == 0:
            names.extend(n for n in out.stdout.splitlines() if n)
    return sorted(set(names))


if __name__ == "__main__":
    sys.exit(main())
