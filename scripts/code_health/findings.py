"""Turn measured values into findings, and measured files into a summary."""

from . import config as _config

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


def _finding(kind, level, subject_id, subject, metric, value, threshold, evidence):
    return {
        "id": "%s:%s" % (kind, subject_id),
        "kind": kind,
        "layer": "L2",
        "level": level,
        "subject": subject,
        "metric": metric,
        "value": value,
        "threshold": threshold,
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
                kind, level, row["path"], {"file": row["path"]}, tkey, value,
                thresholds[tkey][level],
                {"lines": row.get("lines"), "tokens": row.get("tokens")},
            ))

    for row in layer.get("functions", []):
        subject_id = "%s:%s:%s" % (row["filename"], row["function_name"],
                                   row["start_line"])
        subject = {
            "file": row["filename"],
            "function": row["function_name"],
            "start_line": row["start_line"],
            "end_line": row["end_line"],
        }
        for kind, tkey, rkey in _FUNC_RULES:
            value = row.get(rkey)
            if value is None:
                continue
            level = _config.level_for(value, thresholds[tkey])
            if level is None:
                continue
            out.append(_finding(
                kind, level, subject_id, subject, tkey, value,
                thresholds[tkey][level],
                {"ccn": row.get("ccn"), "length": row.get("length"),
                 "param": row.get("param"), "nesting": row.get("nesting"),
                 "lexical_tokens": row.get("token")},
            ))

    out.sort(key=lambda f: f["id"])
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
