"""Size, complexity, parameters and nesting, from lizard plus direct file reads.

Two facts drive this module:
  * `lizard -ENS --csv` emits FUNCTION rows only. A 5000-line file with no
    functions produces empty output and exit 0, so file-level size is read
    directly and never taken from lizard.
  * The `-ENS` extension is what adds the 12th column, `max_nested_structures`.
    Without it there is no nesting data at all, and the threshold field is
    named `max_nested_structures` — `-Tnd=` is rejected.
"""

import csv
import io
import os
import shutil
import subprocess
import sys

from . import config as _config

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
    except (LizardOutputError, OSError) as exc:
        layer["status"] = "partial"
        layer["reason"] = "lizard failed: %s" % exc
    return layer
