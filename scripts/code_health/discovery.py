"""Which files exist, which are source, which are tests, which are neither.

Pattern matching implements gitwildmatch semantics, not fnmatch. This matters:
`**/dist/**` must match top-level `dist/a.js` as well as `packages/web/dist/a.js`,
and neither `fnmatch.fnmatch` nor `PurePath.match` gets that right.

`SOURCE_EXT` is the other half of "which files exist": a tracked, non-binary
file is not automatically source. Documentation, lockfiles and configuration
are text, and measuring them as code puts a 1000-line design document into
`summary.by_cut` and produces `file_too_long` findings nobody can act on --
observed on this plugin's own repository, where 40 `.md` files outnumbered
16 `.py` files and three of three hard file-level findings were prose.
`guard.py` has always filtered by the same set (its `SUPPORTED_EXT`); before
this set existed the audit and the ratchet disagreed about what a file even
is. The two copies are pinned to each other by
`tests/test_parser_agreement.py`, the same way the CSV parser and the
thresholds are.
"""

import collections
import os
import re
import subprocess
from pathlib import Path

Inventory = collections.namedtuple("Inventory", "source tests skipped")

WALK_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__",
    ".venv", "venv", ".tox", ".mypy_cache", ".ruff_cache",
}

BINARY_PROBE_BYTES = 8192

# The one definition of "this file is code". Duplicated verbatim in
# `guard.py` as `SUPPORTED_EXT` -- that file never imports code_health --
# and pinned to this copy by tests/test_parser_agreement.py.
SOURCE_EXT = frozenset({
    ".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".java",
    ".kt", ".rs", ".rb", ".php", ".c", ".h", ".cpp", ".cc", ".hpp",
    ".cs", ".swift", ".scala", ".lua", ".m", ".mm",
})


def compile_pattern(pattern):
    """Translate one gitwildmatch pattern into a regex over a POSIX relpath."""
    anchored = pattern.startswith("/")
    pat = pattern[1:] if anchored else pattern
    # A pattern with no slash matches its basename at any depth.
    if "/" not in pat.rstrip("/"):
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
    out.append("$")
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


def matches_any(relpath, compiled):
    return any(rx.match(relpath) for rx in compiled)


def _git_tracked(repo_root):
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "-z"],
            capture_output=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return [p for p in out.stdout.decode("utf-8", "replace").split("\0") if p]


def _walk(repo_root):
    found = []
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in WALK_SKIP_DIRS]
        for name in filenames:
            abs_path = Path(dirpath) / name
            found.append(abs_path.relative_to(repo_root).as_posix())
    return sorted(found)


def _is_binary(abs_path):
    try:
        with abs_path.open("rb") as handle:
            return b"\0" in handle.read(BINARY_PROBE_BYTES)
    except OSError:
        return True


def enumerate_files(repo_root, cfg, restrict=None):
    """Split the repository into source files, test files, and skip counters."""
    repo_root = Path(repo_root)
    candidates = _git_tracked(repo_root)
    if candidates is None:
        candidates = _walk(repo_root)
    if restrict is not None:
        wanted = set(restrict)
        candidates = [p for p in candidates if p in wanted]

    exclude = [compile_pattern(p) for p in cfg["exclude"]]
    tests_pat = [compile_pattern(p) for p in cfg["tests"]]
    max_bytes = cfg["max_file_bytes"]

    source, tests = [], []
    skipped = {"binary": 0, "too_large": 0, "symlink": 0, "not_source": 0}

    for rel in candidates:
        abs_path = repo_root / rel
        if abs_path.is_symlink():
            skipped["symlink"] += 1
            continue
        if not abs_path.is_file():
            continue
        if matches_any(rel, exclude):
            continue
        if os.path.splitext(rel)[1] not in SOURCE_EXT:
            # Tracked, readable, and not code. Counted rather than dropped
            # silently, so "412 files" and "40 files were not measured as
            # source" are both visible in the same summary.
            skipped["not_source"] += 1
            continue
        try:
            size = abs_path.stat().st_size
        except OSError:
            continue
        if size > max_bytes:
            skipped["too_large"] += 1
            continue
        if _is_binary(abs_path):
            skipped["binary"] += 1
            continue
        (tests if matches_any(rel, tests_pat) else source).append(rel)

    return Inventory(sorted(source), sorted(tests), skipped)
