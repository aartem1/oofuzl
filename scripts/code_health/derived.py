"""Metrics derived from L2: hotspots, symbol collisions, edit fan-out.

These never form their own work items. They re-rank the other groups: the same
complexity is P1 in a file changed 40 times and P3 in one nobody has touched.
"""


def _median(values):
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _ccn_by_file(size_layer):
    totals = {}
    for row in size_layer.get("functions", []):
        totals[row["filename"]] = totals.get(row["filename"], 0) + row.get("ccn", 0)
    return totals


def _percentile_ranks(values):
    ordered = sorted(values)
    ranks = {}
    for value in set(values):
        below = sum(1 for v in ordered if v < value)
        ranks[value] = round(below / float(len(ordered)) * 100, 1) if ordered else 0.0
    return ranks


def _hotspots(size_layer, churn_layer):
    ccn = _ccn_by_file(size_layer)
    commits = {row["path"]: row["commits"] for row in churn_layer.get("files", [])}

    hotspots = []
    for path, commit_count in commits.items():
        complexity = ccn.get(path, 0)
        if complexity == 0:
            continue
        hotspots.append({"path": path, "commits": commit_count,
                         "ccn": complexity, "score": commit_count * complexity})
    commit_ranks = _percentile_ranks([h["commits"] for h in hotspots])
    ccn_ranks = _percentile_ranks([h["ccn"] for h in hotspots])
    for row in hotspots:
        row["churn_pct"] = commit_ranks.get(row["commits"], 0.0)
        row["ccn_pct"] = ccn_ranks.get(row["ccn"], 0.0)
    hotspots.sort(key=lambda row: (-row["score"], row["path"]))
    return hotspots


def _collisions(imports_layer, cfg):
    minimum = cfg["collision_min_files"]
    by_name = {}
    for symbol in imports_layer.get("symbols", []) or []:
        entry = by_name.setdefault(symbol["name"], {"files": set(), "tokens": []})
        entry["files"].add(symbol["file"])
        entry["tokens"].append(symbol.get("tokens", 0))
    collisions = []
    for name, entry in by_name.items():
        if len(entry["files"]) < minimum:
            continue
        avg = sum(entry["tokens"]) / float(len(entry["tokens"]))
        collisions.append({"name": name, "files": len(entry["files"]),
                           "avg_tokens": round(avg, 1),
                           "rank": round(len(entry["files"]) * avg, 1),
                           "paths": sorted(entry["files"])})
    collisions.sort(key=lambda row: (-row["rank"], row["name"]))
    return collisions


def _edit_fanout(churn_layer):
    """`(rows, basis)`. See the mean fallback below for why `basis` exists."""
    others = [max(size - 1, 0) for size in churn_layer.get("commit_sizes", [])]
    baseline = _median(others)
    basis = "median"
    if not baseline:
        # A repository whose typical commit touches exactly one file has a
        # median of zero, and dividing by it would collapse every ratio to 0.0
        # while `mean_co_changed` is genuinely non-zero -- a column of zeros
        # that still looks like data. Measured on this plugin's own history:
        # 35 of 64 commits are single-file. Fall back to the mean, which keeps
        # the comparison meaningful, and say which denominator was used.
        baseline = (sum(others) / float(len(others))) if others else 0.0
        basis = "mean"
    fanout = []
    for row in churn_layer.get("files", []):
        mean_co = row.get("mean_co_changed")
        if mean_co is None:
            continue
        fanout.append({"path": row["path"], "mean_co_changed": round(mean_co, 2),
                       "ratio": round(mean_co / baseline, 2) if baseline else None})
    fanout.sort(key=lambda row: (-(row["ratio"] or 0.0), row["path"]))
    return fanout, (basis if baseline else "unavailable")


def _degraded(value, dep_name, dep_layer, what):
    """`(value, reason)` for one derived metric against the layer it is
    computed FROM.

    An empty result computed from a layer that is not `measured` is not a
    zero, it is an absence: with lizard missing, `size` correctly reports
    `partial` and hands `derived` zero function rows, and `hotspots` then
    came out `[]` under `status: "measured"` -- a confident "this repository
    has no hotspots". Same shape as the churn guard above, applied to the
    other two dependencies.
    """
    if value or dep_layer.get("status") == "measured":
        return value, None
    return None, "%s is %s (%s); %s" % (
        dep_name, dep_layer.get("status"), dep_layer.get("reason"), what)


def measure_derived(size_layer, churn_layer, imports_layer, cfg):
    if churn_layer.get("status") != "measured":
        return {"status": "not_measured",
                "reason": "churn is %s (%s); hotspots and edit fan-out depend on it"
                          % (churn_layer.get("status"), churn_layer.get("reason")),
                "hotspots": None, "symbol_collisions": None, "edit_fanout": None}

    hotspots, hot_reason = _degraded(
        _hotspots(size_layer, churn_layer), "size", size_layer,
        "hotspots need per-function complexity")
    collisions, sym_reason = _degraded(
        _collisions(imports_layer, cfg), "imports", imports_layer,
        "symbol collisions need the symbol table")
    fanout, basis = _edit_fanout(churn_layer)

    reasons = [r for r in (hot_reason, sym_reason) if r]
    return {"status": "partial" if reasons else "measured",
            "reason": "; ".join(reasons) or None,
            "hotspots": hotspots, "symbol_collisions": collisions,
            "edit_fanout": fanout, "edit_fanout_basis": basis}
