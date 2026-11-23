#!/usr/bin/env python3
"""code-health: the single L2+L3 measurer.

Exit code is 0 whenever a valid document was produced, including when every
tool is missing and every layer is not_measured. Findings never change it.
"""

import argparse
import datetime
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from code_health import config as C          # noqa: E402
from code_health import churn as CH          # noqa: E402
from code_health import comments as CM       # noqa: E402
from code_health import derived as DV        # noqa: E402
from code_health import discovery as D       # noqa: E402
from code_health import dup as DUP           # noqa: E402
from code_health import externals as EX      # noqa: E402
from code_health import findings as F        # noqa: E402
from code_health import pyast as PA          # noqa: E402
from code_health import sizes as S           # noqa: E402


def _git(repo_root, *args):
    try:
        out = subprocess.run(["git", "-C", str(repo_root)] + list(args),
                             capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def repo_facts(repo_root):
    head = _git(repo_root, "rev-parse", "HEAD")
    return {
        "root": str(repo_root),
        "head": head,
        "branch": _git(repo_root, "rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(_git(repo_root, "status", "--porcelain=v1")) if head else None,
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="measure.py")
    parser.add_argument("--repo")
    parser.add_argument("--config")
    parser.add_argument("--layers", default=",".join(F.LAYER_NAMES))
    parser.add_argument("--files", nargs="*")
    parser.add_argument("--install-missing", action="store_true")
    parser.add_argument("--update-tools", action="store_true",
                        help="move every tool the layers need to its latest stable release "
                             "(registry lookup, bounded timeout; implies --install-missing); "
                             "the pinned versions stay the fallback when the registry fails")
    parser.add_argument("--out")
    parser.add_argument("--history", metavar="FILE",
                        help="compare with the last line of FILE, then append this run's line")
    parser.add_argument("--compare-history", metavar="FILE",
                        help="compare with the last line of FILE without appending; the audit "
                             "appends with --record-history once its report exists")
    parser.add_argument("--record-history", metavar="FILE",
                        help="append the line of the document given by --from to FILE and exit")
    parser.add_argument("--from", dest="from_doc", metavar="JSON",
                        help="a document written earlier with --out (for --record-history)")
    parser.add_argument("--tool", action="append", default=[], metavar="NAME=VERSION",
                        help="a tool measured outside this script (the SonarQube server, the "
                             "scanner): compared with the history and recorded in its line")
    parser.add_argument("--brief", type=int, metavar="N",
                        help="print totals by kind and the N worst findings of each kind; "
                             "the full document still goes to --out")
    return parser.parse_args(argv)


def build_document(args):
    repo_root = Path(args.repo).resolve()
    if not repo_root.is_dir():
        raise ValueError("--repo is not a directory: %s" % args.repo)
    cfg = C.load_config(repo_root, args.config)
    wanted = F.resolve_layers([n.strip() for n in args.layers.split(",") if n.strip()])
    inventory = D.enumerate_files(repo_root, cfg, restrict=args.files)

    layers = {name: F.empty_layer("layer not requested") for name in F.LAYER_NAMES}
    all_findings = []

    tools = EX.survey(F.tools_for(wanted),
                      install_missing=args.install_missing or args.update_tools,
                      update=args.update_tools)

    if "size" in wanted:
        layers["size"] = S.measure_sizes(repo_root, inventory, cfg)
        all_findings.extend(F.size_findings(layers["size"], cfg))
    if "churn" in wanted:
        present = set(inventory.source) | set(inventory.tests)
        layers["churn"] = CH.measure_churn(repo_root, cfg, present)
    if "dup" in wanted:
        near = DUP.find_near_duplicates(repo_root, layers["size"].get("functions", []), cfg)
        # The other side of a clone may lie outside `--files`, so the whole
        # repository's source list says what counts as code.
        code = inventory.source if args.files is None else \
            D.enumerate_files(repo_root, cfg).source
        exact = EX.run_jscpd(repo_root, cfg, tools["jscpd"], inventory.source, code)
        layers["dup"] = DUP.assemble(layers["size"], near, exact)
        all_findings.extend(F.duplicate_findings(layers["dup"]["near"], cfg))
        all_findings.extend(F.clone_findings(layers["dup"]["exact"], cfg))
    if "imports" in wanted:
        layers["imports"] = PA.measure_imports(repo_root, inventory, cfg)
        js = EX.run_madge(repo_root, list(inventory.source), tools["madge"])
        _merge_js_imports(layers["imports"], js)
        all_findings.extend(F.cycle_findings(layers["imports"]))
    if "dead" in wanted:
        layers["dead"] = EX.run_dead(repo_root, inventory,
                                     {"vulture": tools["vulture"]})
        all_findings.extend(F.dead_findings(layers["dead"]))
    if "derived" in wanted:
        layers["derived"] = DV.measure_derived(
            layers["size"], layers["churn"], layers["imports"], cfg)
        all_findings.extend(F.derived_findings(layers["derived"], cfg))
    if "comments" in wanted:
        layers["comments"] = CM.measure_comments(repo_root, inventory, cfg)
        all_findings.extend(F.comment_findings(layers["comments"]))

    return {
        "schema": 1,
        "generated_at": datetime.datetime.now(datetime.timezone.utc)
                        .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "repo": repo_facts(repo_root),
        "config": cfg,
        "tools": tools,
        "layers": layers,
        "findings": all_findings,
        "summary": F.build_summary(layers["size"], inventory, cfg),
    }


def brief(doc, per_kind):
    """Totals by kind and level plus the `per_kind` worst findings of each
    kind: `hard` first, then the largest value. The full document stays the
    source for the ledger; this is what a model reads first."""
    counts, by_kind = {}, {}
    for finding in doc["findings"]:
        level = counts.setdefault(finding["kind"], {})
        level[finding["level"]] = level.get(finding["level"], 0) + 1
        by_kind.setdefault(finding["kind"], []).append(finding)
    worst = {}
    for kind, rows in sorted(by_kind.items()):
        rows.sort(key=lambda f: (f["level"] != "hard", -_numeric(f.get("value")), f["id"]))
        worst[kind] = rows[:per_kind]
    return {
        "schema": doc["schema"], "generated_at": doc["generated_at"], "repo": doc["repo"],
        "tools": doc["tools"], "summary": doc["summary"],
        "layers": {name: {"status": layer.get("status"), "reason": layer.get("reason")}
                   for name, layer in doc["layers"].items()},
        "counts": counts, "findings": worst,
    }


def history_line(doc, extra_tools=None):
    """One audit as one line: enough to answer "did it get better over a
    year" without keeping every full report."""
    summary = doc["summary"]
    return {
        "at": doc["generated_at"], "head": doc["repo"].get("head"),
        "files": summary.get("files"), "loc": summary.get("loc"),
        "tokens": summary.get("tokens_total"),
        "layers": {name: layer.get("status") for name, layer in doc["layers"].items()},
        "counts": brief(doc, 0)["counts"],
        # Versions of the tools that measured this run: the next audit tells a
        # tool upgrade from a change in the code by comparing them.
        "tools": dict(_versions(doc), **(extra_tools or {})),
    }


def _versions(doc):
    return {name: state["version"] for name, state in sorted(doc["tools"].items())
            if state.get("available") and state.get("version")}


def _extra_tools(pairs):
    out = {}
    for pair in pairs:
        name, sep, version = pair.partition("=")
        if not sep or not name or not version:
            raise ValueError("--tool expects NAME=VERSION: %s" % pair)
        out[name] = version
    return out


def last_history_line(path):
    """The last line of a history file as a dict, or None when the file is
    missing, empty or its last line is not a JSON object."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    except OSError:
        return None
    if not lines:
        return None
    try:
        row = json.loads(lines[-1])
    except ValueError:
        return None
    return row if isinstance(row, dict) else None


def tool_changes(prev_line, doc, extra_tools=None):
    """`[{tool, before, after}]` for every tool whose version differs from the
    previous audit's history line. A tool unknown on either side is not a
    change."""
    before = (prev_line or {}).get("tools")
    if not isinstance(before, dict):
        return []
    changes = []
    for name, now in sorted(dict(_versions(doc), **(extra_tools or {})).items()):
        was = before.get(name)
        if now and was and now != was:
            changes.append({"tool": name, "before": was, "after": now})
    return changes


def tool_changes_note(changes):
    if not changes:
        return None
    return "Инструменты обновились с прошлого аудита: " + ", ".join(
        "%s %s → %s" % (c["tool"], c["before"], c["after"]) for c in changes)


def _numeric(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _merge_js_imports(imports_layer, js):
    """Fold madge's JS/TS result into the Python import layer."""
    if js["status"] != "measured":
        if js.get("reason") and "no JS/TS sources" not in js["reason"]:
            existing = imports_layer.get("reason")
            imports_layer["reason"] = "; ".join(x for x in (existing, js["reason"]) if x)
            if imports_layer["status"] == "measured":
                imports_layer["status"] = "partial"
        return imports_layer

    imports_layer["languages_measured"] = sorted(
        set(imports_layer.get("languages_measured", [])) | {"javascript"})
    imports_layer["cycles"] = list(imports_layer.get("cycles", [])) + js["cycles"]
    fan_in = {}
    for module, targets in js["graph"].items():
        for target in targets:
            fan_in[target] = fan_in.get(target, 0) + 1
    imports_layer["degree"] = list(imports_layer.get("degree", [])) + [
        {"module": module, "fan_out": len(targets), "fan_in": fan_in.get(module, 0)}
        for module, targets in sorted(js["graph"].items())
    ]
    if imports_layer["status"] == "not_measured":
        imports_layer["status"] = "measured"
        imports_layer["reason"] = None
    return imports_layer


def _append_history(path, doc, extra):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(history_line(doc, extra), ensure_ascii=False, sort_keys=True) + "\n")


def main(argv=None):
    args = parse_args(argv)
    try:
        extra = _extra_tools(args.tool)
        if args.record_history:
            if not args.from_doc:
                raise ValueError("--record-history needs --from <document>")
            _append_history(args.record_history,
                            json.loads(Path(args.from_doc).read_text(encoding="utf-8")), extra)
            sys.stdout.write(json.dumps({"recorded": args.record_history}) + "\n")
            return 0
        if not args.repo:
            raise ValueError("--repo is required")
        doc = build_document(args)
    except (C.ConfigError, ValueError, OSError) as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2
    text = json.dumps(doc, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    compare = args.history or args.compare_history
    changes = tool_changes(last_history_line(compare), doc, extra) if compare else []
    if args.history:
        _append_history(args.history, doc, extra)
    if args.brief is not None:
        summary = brief(doc, args.brief)
        if changes:
            summary["tool_changes"] = changes
            summary["tool_changes_note"] = tool_changes_note(changes)
        sys.stdout.write(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    elif not args.out:
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
