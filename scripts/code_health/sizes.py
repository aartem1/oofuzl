"""Size, complexity, parameters and nesting, from lizard plus direct file reads.

Two facts drive this module:
  * `lizard -ENS --csv` emits FUNCTION rows only. A 5000-line file with no
    functions produces empty output and exit 0, so file-level size is read
    directly and never taken from lizard.
  * The `-ENS` extension is what adds the 12th column, `max_nested_structures`.
    Without it there is no nesting data at all, and the threshold field is
    named `max_nested_structures` — `-Tnd=` is rejected.

A third fact drives the nesting column specifically, verified with a
flat-vs-deep probe (`references/tools.md`): lizard's nesting reader resets
on a closing brace, so it is correct for brace/semicolon languages and wrong
-- a monotonically increasing, cross-function, cross-file counter -- for
Python (indentation-based) and every other reader with nothing to key a
reset on. `_apply_nesting_source` below is what corrects this after parsing.
"""

import csv
import io
import os
import shutil
import subprocess
import sys

from . import config as _config
from . import pyast as _pyast

LIZARD_VERSION = "1.23.0"
LIZARD_ENV_OVERRIDE = "CODE_HEALTH_LIZARD"

FUNC_FIELDS = (
    "nloc", "ccn", "token", "param", "length", "location",
    "filename", "function_name", "long_name", "start_line", "end_line",
    "nesting",
)
_INT_FIELDS = {"nloc", "ccn", "token", "param", "length", "start_line",
               "end_line", "nesting"}

# Chunked so a large repository does not exceed the OS argv limit.
_ARG_CHUNK = 400

# Extensions whose lizard reader was verified, with a flat-vs-deep probe, to
# reset its nesting counter correctly: a 5-sequential-`if` function reports
# nesting 1 and a 5-deep-nested-`if` function reports 5, for every one of
# these. All are brace/semicolon readers, matching `lizardnd.py`'s
# `bracket = '}'` default reset. Kept as lizard's own value, unchanged.
#
# Verified BROKEN by the same probe and therefore never trusted from
# lizard: `.py` (indentation, corrected below from the AST instead),
# `.rb` (trailing-`if`/`unless` modifiers keep accumulating), `.lua`
# (`then`/`end`, no bracket to reset on). Any extension not in this set and
# not `.py` gets nesting set to `None` -- "not measured", never a guess.
_LIZARD_NESTING_OK_EXT = {
    ".c", ".h", ".cpp", ".cc", ".hpp",
    ".cs", ".java",
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
    ".go", ".kt", ".rs", ".swift", ".scala", ".php",
    ".m", ".mm",
}


class LizardOutputError(Exception):
    pass


def parse_lizard_csv(text):
    rows = []
    for raw in csv.reader(io.StringIO(text)):
        if not raw:
            continue
        if len(raw) != len(FUNC_FIELDS):
            raise LizardOutputError(
                "expected %d columns (lizard -ENS --csv), got %d: %r"
                % (len(FUNC_FIELDS), len(raw), raw)
            )
        row = dict(zip(FUNC_FIELDS, raw))
        for field in _INT_FIELDS:
            row[field] = int(row[field])
        rows.append(row)
    return rows


def resolve_lizard():
    """argv prefix for invoking lizard, or None. Never `import lizard`."""
    override = os.environ.get(LIZARD_ENV_OVERRIDE)
    if override:
        return [override]
    found = shutil.which("lizard")
    if found:
        return [found]
    probe = subprocess.run(
        [sys.executable, "-m", "lizard", "--version"],
        capture_output=True,
    )
    if probe.returncode == 0:
        return [sys.executable, "-m", "lizard"]
    return None


def run_lizard(repo_root, relpaths, argv_prefix=None):
    argv_prefix = argv_prefix or resolve_lizard()
    if argv_prefix is None:
        raise LizardOutputError("lizard is not available")
    chunks = []
    for start in range(0, len(relpaths), _ARG_CHUNK):
        batch = relpaths[start:start + _ARG_CHUNK]
        proc = subprocess.run(
            argv_prefix + ["-ENS", "--csv"] + batch,
            cwd=str(repo_root), capture_output=True, text=True,
        )
        # lizard exits non-zero when thresholds are exceeded; we set none, but
        # stay tolerant and rely on stdout being parseable.
        chunks.append(proc.stdout)
    return "".join(chunks)


def measure_files(repo_root, relpaths, cfg):
    chars_per_token = cfg["chars_per_token"]
    out = []
    for rel in relpaths:
        try:
            text = (repo_root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        out.append({
            "path": rel,
            "lines": len(lines),
            "code_lines": sum(1 for line in lines if line.strip()),
            "chars": len(text),
            "tokens": _config.estimate_tokens(text, chars_per_token),
        })
    return out


def _apply_nesting_source(repo_root, functions, targets):
    """Correct lizard's `nesting` column in place; see the module docstring.

    For `.py` rows, `nesting` is replaced with the exact value from
    `pyast.measure_nesting`, joined by `(file, start_line)` -- verified the
    reliable half of the key: a nested/decorated function's *name* does not
    reliably line up (lizard qualifies it, e.g. `outer.inner`, while `ast`
    reports the bare `inner`), but its `start_line` does, including through
    decorators and multi-line decorators. A `.py` row whose start line has
    no match in the AST pass (should be rare; not observed in testing, but
    the join is not proven exhaustive) gets `nesting = None` rather than
    lizard's uncorrected, potentially-accumulating number.

    For every other extension, lizard's value is kept only when verified
    correct (`_LIZARD_NESTING_OK_EXT`); everything else becomes `None`.

    Returns a small summary dict recorded on the layer, so a consumer can
    tell "measured, matched" from "measured, unmatched" from "not measured".
    """
    python_rows, errors = _pyast.measure_nesting(repo_root, targets)
    by_key = {}
    for row in python_rows:
        by_key.setdefault((row["file"], row["start_line"]), row["nesting"])

    matched = unmatched = kept = dropped = 0
    for row in functions:
        ext = os.path.splitext(row["filename"])[1]
        if ext == ".py":
            key = (row["filename"], row["start_line"])
            if key in by_key:
                row["nesting"] = by_key[key]
                matched += 1
            else:
                row["nesting"] = None
                unmatched += 1
        elif ext in _LIZARD_NESTING_OK_EXT:
            kept += 1
        else:
            row["nesting"] = None
            dropped += 1

    return {
        "python_matched": matched,
        "python_unmatched": unmatched,
        "kept_from_lizard": kept,
        "not_measured": dropped,
        "python_parse_errors": errors,
    }


def measure_sizes(repo_root, inventory, cfg, include_tests=False):
    """The layers.size object. Always returns a status; never raises."""
    targets = list(inventory.source)
    if include_tests:
        targets += list(inventory.tests)

    layer = {
        "status": "measured",
        "files": measure_files(repo_root, targets, cfg),
        "test_files": measure_files(repo_root, list(inventory.tests), cfg),
        "functions": [],
    }

    argv_prefix = resolve_lizard()
    if argv_prefix is None:
        layer["status"] = "partial"
        layer["reason"] = (
            "lizard is not available: file sizes measured, function metrics not measured"
        )
        return layer

    try:
        text = run_lizard(repo_root, targets, argv_prefix)
        layer["functions"] = parse_lizard_csv(text)
        layer["nesting_source"] = _apply_nesting_source(repo_root, layer["functions"], targets)
    except (LizardOutputError, OSError) as exc:
        layer["status"] = "partial"
        layer["reason"] = "lizard failed: %s" % exc
    return layer
