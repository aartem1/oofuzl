"""Turn measured values into findings, and measured files into a summary."""

import hashlib
from collections import namedtuple

from . import config as _config

# What a subject was measured at, against what it must stay under -- the
# three numbers a rule check produces together, always used together.
Measurement = namedtuple("Measurement", "metric value threshold")

LAYER_NAMES = ("size", "dup", "imports", "dead", "churn", "derived")
# `derived` reads `imports_layer["symbols"]` for symbol collisions, so
# `imports` is a real dependency and not declaring it made
# `measure.py --layers derived` yield no collisions at all -- which is
# exactly what `fix` step 3.1 runs before deciding a group's findings are
# gone. Every `symbol_collision` group was therefore skipped unconditionally.
LAYER_DEPS = {"dup": ("size",), "derived": ("size", "churn", "imports")}

# The external tools each layer can actually use. `survey` probes only the
# union over the RESOLVED layers: probing all five costs ~3.4 s of
# `npx --no-install` startup for tools the requested layer cannot consume,
# and `fix` runs a narrow `measure.py` twice per queue item. `churn` uses
# plain git and `derived` only reads other layers, so both need nothing;
# `dead` needs vulture alone, because knip's output is not parsed (see
# externals.run_dead).
LAYER_TOOLS = {
    "size": ("lizard",),
    "dup": ("jscpd",),
    "imports": ("madge",),
    "dead": ("vulture",),
    "churn": (),
    "derived": (),
}


def tools_for(layers):
    """Every tool the given (already resolved) layers can use, sorted."""
    return sorted({tool for layer in layers for tool in LAYER_TOOLS.get(layer, ())})

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


def _cluster_id(cluster):
    """`cluster/<8 hex>` over the cluster's sorted membership.

    The old form was `cluster/<size>/<members[0].filename>`, and `members`
    is not sorted -- the id depended on the order the clustering pass
    happened to emit its members in, so two runs over identical code could
    disagree. `references/ledger.md`'s own example is `cluster/7f3a91c4`,
    which this now matches, and the digest is content-derived exactly like
    a group's `gid`: the same code always produces the same id, and a
    cluster whose membership genuinely changed is a different cluster,
    which is what makes a vanished one detectable rather than silently
    substituted.
    """
    members = sorted("%s:%s" % (m["filename"], m["function_name"])
                     for m in cluster.get("members", []))
    digest = hashlib.sha1("\n".join(members).encode("utf-8")).hexdigest()
    return "cluster/%s" % digest[:8]


def duplicate_findings(near, cfg):
    out = []
    for cluster in (near or {}).get("clusters", []):
        out.append({
            "id": "near_duplicate:%s" % _cluster_id(cluster),
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


def clone_findings(clones, cfg):
    """`exact_clone` findings from jscpd's duplicate blocks.

    A documented finding kind (DESIGN 5 and 8, `references/metrics.md`,
    `references/quality-policy.md`, `skills/audit/SKILL.md`) that used to
    land in `layers.dup.exact` and become nothing, so the audit's
    "`near_duplicate`, `exact_clone` -> one group per cluster" row could
    only ever fire on half its inputs.
    """
    out = []
    for clone in clones or []:
        places = _clone_places(clone)
        if not places:
            continue
        out.append({
            "id": "exact_clone:%s" % "|".join(places),
            "kind": "exact_clone", "layer": "L2", "level": "warn",
            "subject": {"files": sorted({p.rsplit(":", 1)[0] for p in places}),
                        "places": places},
            "metric": "exact_clone", "value": clone.get("tokens"),
            "threshold": cfg["near_duplicate"]["min_tokens"],
            "evidence": {"lines": clone.get("lines"), "tokens": clone.get("tokens"),
                         "format": clone.get("format")},
        })
    out.sort(key=lambda f: f["id"])
    return out


def _clone_places(clone):
    """`file:start_line` for each side of one jscpd duplicate, sorted so the
    id does not depend on which side jscpd called first."""
    places = []
    for side in ("firstFile", "secondFile"):
        entry = clone.get(side) or {}
        name = entry.get("name")
        if name:
            places.append("%s:%s" % (name, entry.get("start")))
    return sorted(places)


def dead_findings(layer):
    """`dead_symbol` findings from vulture/knip results.

    Same story as `exact_clone`: documented everywhere, emitted nowhere.
    The id keys on the symbol NAME rather than its line, so it survives an
    edit elsewhere in the file, and the tool's own confidence travels with
    the finding because both tools have known false positives on dynamic
    dispatch (DESIGN 5).
    """
    out = []
    for row in layer.get("symbols") or []:
        name = row.get("name") or "line %s" % row.get("line")
        out.append({
            "id": "dead_symbol:%s:%s" % (row.get("file"), name),
            "kind": "dead_symbol", "layer": "L2", "level": "warn",
            "subject": {"file": row.get("file"), "symbol": row.get("name"),
                        "line": row.get("line")},
            "metric": "dead_symbol", "value": row.get("confidence"),
            "threshold": None,
            "evidence": {"message": row.get("message"), "tool": row.get("tool")},
        })
    out.sort(key=lambda f: f["id"])
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
    for row in layer.get("symbol_collisions") or []:
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
