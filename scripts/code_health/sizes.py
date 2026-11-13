"""Size, complexity, parameters and nesting, from lizard plus direct file reads.

Two facts drive this module:
  * `lizard -ENS --csv` emits FUNCTION rows only. A 5000-line file with no
    functions produces empty output and exit 0, so file-level size is read
    directly and never taken from lizard.
  * The `-ENS` extension is what adds the 12th column, `max_nested_structures`.
    Without it there is no nesting data at all, and the threshold field is
    named `max_nested_structures` — `-Tnd=` is rejected.

A third fact drives the nesting column specifically: lizard's nesting
counter is not reset between files of one run. For Python it only ever grows;
for brace languages it holds within a clean file but carries over after a
file its reader gets wrong -- an ordinary React component is enough
(tests/test_sizes.py). A number that depends on which file came before is
not a measurement, so `_apply_nesting_source` takes Python nesting from the
AST and reports every other language as not measured.
"""

import csv
import io
import os
import shutil
import subprocess
import sys

from . import config as _config
from .discovery import FILE_ONLY_EXT
from . import pyast as _pyast

# The pinned lizard version lives in `externals.LIZARD_VERSION` alone --
# it is what provisioning and the pre-commit `additional_dependencies`
# entry both read. A second copy here was unused and free to drift.
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

# Same function as guard.py, pinned by tests/test_parser_agreement.py.
JS_EXT = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}
_PAIRS = {"(": ")", "[": "]", "{": "}"}
_SIGNATURE_LINES = 30


def destructured_params(lines, start_line, counted):
    """lizard's parameter count, with a destructured object or array as one.

    lizard counts every field of `({ title, text, icon }: Props)` as a
    parameter, so a React component's props count read as its parameters.
    Reads the first parenthesized list from the function's first line; when
    one of its top-level items is `{...}` or `[...]`, returns the number of
    top-level items. Never returns more than lizard counted.
    """
    text = "\n".join(lines[start_line - 1:start_line - 1 + _SIGNATURE_LINES])
    start = text.find("(")
    if start == -1:
        return counted
    items, depth, item, i = [], 0, "", start + 1
    while i < len(text):
        ch = text[i]
        if ch in "\"'`":
            end = text.find(ch, i + 1)
            if end == -1:
                return counted
            item, i = item + text[i:end + 1], end + 1
            continue
        if ch == ")" and depth == 0:
            items.append(item.strip())
            break
        if ch in _PAIRS:
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            items.append(item.strip())
            item = ""
        else:
            item += ch
        i += 1
    else:
        return counted
    items = [x for x in items if x]
    if not any(x[:1] in "{[" for x in items):
        return counted
    return min(counted, len(items))


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

    Every other extension becomes `None`: lizard's value there depends on
    the files measured before it in the same run (module docstring).

    Returns a small summary dict recorded on the layer, so a consumer can
    tell "measured, matched" from "measured, unmatched" from "not measured".
    """
    python_rows, errors = _pyast.measure_nesting(repo_root, targets)
    by_key = {}
    for row in python_rows:
        by_key.setdefault((row["file"], row["start_line"]), row)

    matched = unmatched = dropped = 0
    for row in functions:
        ext = os.path.splitext(row["filename"])[1]
        if ext == ".py":
            key = (row["filename"], row["start_line"])
            if key in by_key:
                row["nesting"] = by_key[key]["nesting"]
                row["qualname"] = by_key[key]["qualname"]
                matched += 1
            else:
                row["nesting"] = None
                unmatched += 1
        else:
            row["nesting"] = None
            dropped += 1

    return {
        "python_matched": matched,
        "python_unmatched": unmatched,
        "not_measured": dropped,
        "python_parse_errors": errors,
    }


def _apply_destructured_params(repo_root, functions):
    """Correct lizard's `param` for JS/TS rows in place (`destructured_params`)."""
    lines = {}
    for row in functions:
        if os.path.splitext(row["filename"])[1] not in JS_EXT:
            continue
        if row["filename"] not in lines:
            try:
                lines[row["filename"]] = (repo_root / row["filename"]).read_text(
                    encoding="utf-8", errors="replace").splitlines()
            except OSError:
                lines[row["filename"]] = []
        row["param"] = destructured_params(lines[row["filename"]], row["start_line"],
                                           row["param"])


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
        text = run_lizard(repo_root, [t for t in targets
                                      if os.path.splitext(t)[1] not in FILE_ONLY_EXT], argv_prefix)
        layer["functions"] = parse_lizard_csv(text)
        _apply_destructured_params(repo_root, layer["functions"])
        layer["nesting_source"] = _apply_nesting_source(repo_root, layer["functions"], targets)
    except (LizardOutputError, OSError) as exc:
        layer["status"] = "partial"
        layer["reason"] = "lizard failed: %s" % exc
    return layer
