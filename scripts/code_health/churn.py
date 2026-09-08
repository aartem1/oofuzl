"""How often each file changes, from plain git.

The command shape matters and each flag earns its place:
  git -C <repo>            -- `-- .` would be cwd-relative and silently wrong
  --no-merges              -- merge commits are not edits
  --numstat -z             -- without -z, -M emits `src/{old => new}/mod.py`,
                              which no split(" => ") can fold correctly
  -M                       -- follow renames
  --format=%H\\t%aI\\t%aE    -- %aI is the author DATE; %aE is the identity, and
                              without it there is no author information at all
With -z there is no blank line between the header and the numstat block;
every record is NUL-terminated instead.
"""

import datetime
import re
import subprocess
from pathlib import Path

# A header record starts with a SHA (40-hex in real git, or any non-numeric string
# in tests) followed by a tab. Numstat rows start with added/removed counts
# (digits or "-"), so we can distinguish by checking if the first field is numeric.
def _is_header(record):
    if not record or "\t" not in record:
        return False
    first_field = record.split("\t", 1)[0]
    # Headers have non-numeric first field (SHA), numstat rows have numeric/dash
    return not (first_field.isdigit() or first_field == "-")


def parse_log(payload):
    records = [r for r in payload.split("\0") if r != ""]
    commits = []
    per_file = {}
    current = None
    pending_rename = None
    renames = {}  # Track old_path -> new_path for renaming older edits

    index = 0
    while index < len(records):
        record = records[index]
        if pending_rename is None and _is_header(record):
            sha, date, email = record.split("\t")
            current = {"sha": sha, "date": date, "email": email, "paths": []}
            commits.append(current)
            index += 1
            continue

        if pending_rename is not None:
            # Two path records follow a numstat row whose path field was empty.
            old_path, new_path = pending_rename["old"], record
            renames[old_path] = new_path
            _record(per_file, current, new_path, pending_rename["added"],
                    pending_rename["removed"])
            per_file.pop(old_path, None)
            current["paths"].append(new_path)
            pending_rename = None
            index += 1
            continue

        parts = record.split("\t")
        if len(parts) < 3:
            index += 1
            continue
        added, removed, path = parts[0], parts[1], "\t".join(parts[2:])
        added_n = 0 if added == "-" else int(added)
        removed_n = 0 if removed == "-" else int(removed)
        if path == "":
            # Rename: the next two records are the old and new paths.
            pending_rename = {"added": added_n, "removed": removed_n,
                              "old": records[index + 1]}
            index += 2
            continue
        # Use new path if this old path was renamed in a newer commit
        canonical_path = renames.get(path, path)
        _record(per_file, current, canonical_path, added_n, removed_n)
        current["paths"].append(path)
        index += 1

    for stats in per_file.values():
        stats["authors"] = len(stats.pop("_authors"))
    return commits, per_file


def _record(per_file, commit, path, added, removed):
    stats = per_file.setdefault(path, {
        "commits": 0, "_authors": set(), "lines_added": 0,
        "lines_removed": 0, "last_change": None,
    })
    stats["commits"] += 1
    stats["_authors"].add(commit["email"])
    stats["lines_added"] += added
    stats["lines_removed"] += removed
    if stats["last_change"] is None or commit["date"] > stats["last_change"]:
        stats["last_change"] = commit["date"]


def _history_span_days(commits):
    dates = []
    for commit in commits:
        try:
            dates.append(datetime.datetime.fromisoformat(commit["date"]))
        except ValueError:
            continue
    if len(dates) < 2:
        return 0
    return (max(dates) - min(dates)).days


def measure_churn(repo_root, cfg, present_paths):
    repo_root = Path(repo_root)
    if not (repo_root / ".git").exists():
        return {"status": "not_measured",
                "reason": "no git repository: churn and every churn-derived metric "
                          "are not measured"}
    window = cfg["churn_window_days"]
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "log",
             "--since=%d days ago" % window, "--no-merges", "--numstat", "-z", "-M",
             "--format=%H%x09%aI%x09%aE"],
            capture_output=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        return {"status": "not_measured", "reason": "git log failed: %s" % exc}

    commits, per_file = parse_log(proc.stdout.decode("utf-8", "replace"))
    rows = [dict(stats, path=path) for path, stats in per_file.items()
            if path in present_paths]
    rows.sort(key=lambda row: (-row["commits"], row["path"]))
    return {
        "status": "measured",
        "window_days": window,
        "history_span_days": _history_span_days(commits),
        "commits": len(commits),
        "files": rows,
        "commit_sizes": [len(commit["paths"]) for commit in commits],
    }
