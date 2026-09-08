"""Turn measured values into findings, and measured files into a summary."""

from collections import namedtuple

from . import config as _config

# What a subject was measured at, against what it must stay under -- the
# three numbers a rule check produces together, always used together.
Measurement = namedtuple("Measurement", "metric value threshold")

LAYER_NAMES = ("size", "dup", "imports", "dead", "churn", "derived")
LAYER_DEPS = {"dup": ("size",), "derived": ("size", "churn")}

# (finding kind, threshold key, row key)
_FILE_RULES = (
    ("file_too_long", "file_lines", "lines"),
    ("file_too_expensive", "file_tokens", "tokens"),
)
_FUNC_RULES = (
    ("function_too_long", "function_lines", "length"),
    ("function_too_complex", "function_ccn", "ccn"),
    ("too_many_params", "parameters", "param"),
    ("too_deep_nesting", "nesting", "nesting"),
)


def empty_layer(reason):
    return {"status": "not_measured", "reason": reason}


def resolve_layers(requested):
    wanted = set()
    pending = list(requested)
    while pending:
        name = pending.pop()
        if name not in LAYER_NAMES:
            raise ValueError("unknown layer %r; known: %s"
                             % (name, ", ".join(LAYER_NAMES)))
        if name in wanted:
            continue
        wanted.add(name)
        pending.extend(LAYER_DEPS.get(name, ()))
    return wanted


def _subject_id(subject):
    """The finding id's subject half: a function's `file:function:start_line`
    (the join key `sizes.py`/`pyast.py` use elsewhere), or a file's bare
    path when there is no function."""
    if "function" in subject:
        return "%s:%s:%s" % (subject["file"], subject["function"], subject["start_line"])
    return subject["file"]


def _finding(kind, level, subject, measurement, evidence):
    return {
        "id": "%s:%s" % (kind, _subject_id(subject)),
        "kind": kind,
        "layer": "L2",
        "level": level,
        "subject": subject,
        "metric": measurement.metric,
        "value": measurement.value,
        "threshold": measurement.threshold,
        "evidence": evidence,
    }


def size_findings(layer, cfg):
    thresholds = cfg["thresholds"]
    out = []

    for row in layer.get("files", []):
        for kind, tkey, rkey in _FILE_RULES:
            value = row.get(rkey)
            if value is None:
                continue
            level = _config.level_for(value, thresholds[tkey])
            if level is None:
                continue
            out.append(_finding(
                kind, level, {"file": row["path"]},
                Measurement(tkey, value, thresholds[tkey][level]),
                {"lines": row.get("lines"), "tokens": row.get("tokens")},
            ))

    for row in layer.get("functions", []):
        subject = {
            "file": row["filename"],
            "function": row["function_name"],
            "start_line": row["start_line"],
            "end_line": row["end_line"],
        }
        for kind, tkey, rkey in _FUNC_RULES:
            value = row.get(rkey)
            # `nesting` is `None` for a language sizes.py could not verify
            # lizard's nesting reader for (and for an unmatched Python row);
            # `None` never reaches `_config.level_for`, which would crash
            # comparing it, and it never produces `too_deep_nesting` --
            # "not measured" must never look like "zero".
            if value is None:
                continue
            level = _config.level_for(value, thresholds[tkey])
            if level is None:
                continue
            out.append(_finding(
                kind, level, subject,
                Measurement(tkey, value, thresholds[tkey][level]),
                {"ccn": row.get("ccn"), "length": row.get("length"),
                 "param": row.get("param"), "nesting": row.get("nesting"),
                 "lexical_tokens": row.get("token")},
            ))

    out.sort(key=lambda f: f["id"])
    return out


def duplicate_findings(near, cfg):
    out = []
    for index, cluster in enumerate(near.get("clusters", [])):
        subject_id = "cluster/%d/%s" % (
            cluster["size"],
            cluster["members"][0]["filename"] if cluster["members"] else index,
        )
        out.append({
            "id": "near_duplicate:%s" % subject_id,
            "kind": "near_duplicate",
            "layer": "L2",
            "level": "warn",
            "subject": {"files": sorted({m["filename"] for m in cluster["members"]}),
                        "functions": [m["function_name"] for m in cluster["members"]]},
            "metric": "near_duplicate",
            "value": cluster["min_lexical"],
            "threshold": cfg["near_duplicate"]["min_similarity"],
            "evidence": {"size": cluster["size"], "avg_tokens": cluster["avg_tokens"],
                         "max_lexical": cluster["max_lexical"],
                         "min_structural": cluster["min_structural"]},
        })
    return out


def cycle_findings(layer):
    out = []
    for cycle in layer.get("cycles", []):
        out.append({
            "id": "import_cycle:%s" % "|".join(cycle),
            "kind": "import_cycle", "layer": "L2", "level": "warn",
            "subject": {"modules": cycle},
            "metric": "import_cycle", "value": len(cycle), "threshold": 2,
            "evidence": {"modules": cycle},
        })
    return out


def derived_findings(layer, cfg):
    out = []
    for row in layer.get("symbol_collisions", []):
        out.append({
            "id": "symbol_collision:%s" % row["name"],
            "kind": "symbol_collision", "layer": "L3", "level": "warn",
            "subject": {"symbol": row["name"], "files": row["paths"]},
            "metric": "symbol_collision", "value": row["files"],
            "threshold": cfg["collision_min_files"],
            "evidence": {"avg_tokens": row["avg_tokens"]},
        })
    return out


def build_summary(layer, inventory, cfg):
    files = layer.get("files", [])
    loc = sum(row["lines"] for row in files)
    tokens_total = sum(row["tokens"] for row in files)

    by_cut = {}
    for cut in _config.line_cuts(cfg):
        hit = [row for row in files if row["lines"] >= cut]
        cut_loc = sum(row["lines"] for row in hit)
        by_cut[str(cut)] = {
            "files": len(hit),
            "loc": cut_loc,
            "loc_pct": round(cut_loc / loc * 100, 1) if loc else 0.0,
        }

    test_files = layer.get("test_files", [])
    return {
        "files": len(files),
        "loc": loc,
        "tokens_total": tokens_total,
        "tests": {"files": len(test_files),
                  "loc": sum(row["lines"] for row in test_files)},
        "skipped": dict(inventory.skipped),
        "by_cut": by_cut,
    }
