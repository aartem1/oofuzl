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


def measure(repo_root, relpaths, chars_per_token):
    repo_root = Path(repo_root)
    files = {}
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
    argv = resolve_lizard()
    if argv is None or not files:
        return files, argv is not None
    proc = subprocess.run(argv + ["-ENS", "--csv"] + list(files),
                          cwd=str(repo_root), capture_output=True, text=True)
    for row in parse_lizard_csv(proc.stdout):
        entry = files.get(row["filename"])
        if entry is None:
            continue
        entry["functions"][row["function_name"]] = {
            "lines": row["length"], "ccn": row["ccn"],
            "params": row["param"], "nesting": row["nesting"],
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
        for label, key, tkey in _FILE_CHECKS:
            warn = thresholds[tkey]["warn"]
            limit = ceiling(base.get(key), warn)
            if exceeds(current[key], limit):
                violations.append({
                    "path": path, "entity": None, "metric": label,
                    "value": current[key], "limit": limit,
                    "baseline": base.get(key), "warn": warn,
                })
        base_funcs = base.get("functions", {})
        for name, func in sorted(current["functions"].items()):
            base_func = base_funcs.get(name, {})
            for label, key, tkey in _FUNC_CHECKS:
                warn = thresholds[tkey]["warn"]
                limit = ceiling(base_func.get(key), warn)
                if exceeds(func[key], limit):
                    violations.append({
                        "path": path, "entity": name, "metric": label,
                        "value": func[key], "limit": limit,
                        "baseline": base_func.get(key), "warn": warn,
                    })
    return violations


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
