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
import fnmatch
import io
import json
import math
import os
import shutil
import subprocess
import sys
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


def _excluded(relpath, patterns):
    """Segment-level subset of discovery.compile_pattern, good enough for a hook.

    `**/dist/**` -> skip any path with a `dist` segment. Patterns that are not
    of that shape fall back to a whole-path fnmatch.
    """
    segments = set(Path(relpath).parts)
    for pattern in patterns:
        core = pattern.strip("/")
        if core.startswith("**/"):
            core = core[3:]
        if core.endswith("/**"):
            core = core[:-3]
        if "/" not in core and "*" not in core:
            if core in segments:
                return True
        elif fnmatch.fnmatch(relpath, pattern) or fnmatch.fnmatch(relpath, core):
            return True
    return False


def settings_for(repo_root):
    cfg = load_json(Path(repo_root) / CONFIG_RELPATH, {})
    merged = {k: dict(v) for k, v in DEFAULT_THRESHOLDS.items()}
    for name, override in (cfg.get("thresholds") or {}).items():
        if name in merged:
            merged[name].update(override)
    return merged, cfg.get("chars_per_token", DEFAULT_CHARS_PER_TOKEN), cfg.get("exclude", [])


def _read_files(repo_root, relpaths, chars_per_token):
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
            "functions": {},
        }
        if rel.endswith(".py"):
            python_nesting[rel] = _python_nesting(text)
    return files, python_nesting


def _lizard_rows(repo_root, argv, relpaths):
    proc = subprocess.run(argv + ["-ENS", "--csv"] + list(relpaths),
                          cwd=str(repo_root), capture_output=True, text=True)
    return parse_lizard_csv(proc.stdout)


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


def measure(repo_root, relpaths, chars_per_token):
    repo_root = Path(repo_root)
    files, python_nesting = _read_files(repo_root, relpaths, chars_per_token)
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


# (label, measured key, threshold key)
_FILE_CHECKS = (("lines", "lines", "file_lines"),
                ("tokens", "tokens", "file_tokens"))
_FUNC_CHECKS = (("lines", "lines", "function_lines"),
                ("ccn", "ccn", "function_ccn"),
                ("params", "params", "parameters"),
                ("nesting", "nesting", "nesting"))


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
        value = values[key]
        # `nesting` is `None` for a language never verified safe to trust
        # lizard's nesting reader for (or an unmatched Python row) -- not
        # checked at all, never checked against a wrong number. File checks
        # never see `None` here, so this guards the function checks only.
        if value is None:
            continue
        warn = thresholds[tkey]["warn"]
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
        print("  %-48s %-8s %s > %s (%s)"
              % (where, v["metric"], v["value"], v["limit"], origin))
    print("\nOld debt is grandfathered; this is new growth.")
    print("To move the ratchet deliberately:  /code-health:guard update-baseline")
    print("To bypass this one commit:         git commit --no-verify")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="guard.py")
    parser.add_argument("files", nargs="*")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--baseline")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo).resolve()
    thresholds, chars_per_token, exclude = settings_for(repo_root)

    if args.baseline:
        targets = _all_supported(repo_root, exclude)
        files, had_lizard = measure(repo_root, targets, chars_per_token)
        if not had_lizard:
            sys.stderr.write("code-health guard: lizard not available\n")
            return 2
        Path(args.baseline).parent.mkdir(parents=True, exist_ok=True)
        Path(args.baseline).write_text(json.dumps({
            "schema": 1,
            "head": _head(repo_root),
            "thresholds": thresholds,
            "files": files,
        }, indent=2, sort_keys=True), encoding="utf-8")
        print("code-health guard: baseline written for %d file(s)" % len(files))
        return 0

    targets = _changed(repo_root) if args.check else list(args.files)
    targets = [t for t in targets
               if Path(t).suffix in SUPPORTED_EXT and not _excluded(t, exclude)]
    if not targets:
        return 0

    baseline_path = repo_root / BASELINE_RELPATH
    if not baseline_path.exists():
        print("code-health guard: no baseline at %s; nothing to ratchet against. "
              "Run /code-health:guard install." % BASELINE_RELPATH)
        return 0

    baseline = load_json(baseline_path, {"files": {}})
    measured, had_lizard = measure(repo_root, targets, chars_per_token)
    if not had_lizard:
        print("code-health guard: lizard unavailable; file sizes checked only")
    violations = compare(measured, baseline, thresholds)
    if not violations:
        return 0
    report(violations)
    return 1


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
            if Path(n).suffix in SUPPORTED_EXT and not _excluded(n, exclude)]


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
