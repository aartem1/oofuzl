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
from code_health import discovery as D       # noqa: E402
from code_health import findings as F        # noqa: E402
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
    parser.add_argument("--repo", required=True)
    parser.add_argument("--config")
    parser.add_argument("--layers", default=",".join(F.LAYER_NAMES))
    parser.add_argument("--files", nargs="*")
    parser.add_argument("--install-missing", action="store_true")
    parser.add_argument("--out")
    return parser.parse_args(argv)


def build_document(args):
    repo_root = Path(args.repo).resolve()
    cfg = C.load_config(repo_root, args.config)
    wanted = F.resolve_layers([n.strip() for n in args.layers.split(",") if n.strip()])
    inventory = D.enumerate_files(repo_root, cfg, restrict=args.files)

    layers = {name: F.empty_layer("layer not requested") for name in F.LAYER_NAMES}
    all_findings = []

    if "size" in wanted:
        layers["size"] = S.measure_sizes(repo_root, inventory, cfg)
        all_findings.extend(F.size_findings(layers["size"], cfg))
    # Tasks 6-10 replace the remaining not_measured placeholders.
    for name in ("dup", "imports", "dead", "churn", "derived"):
        if name in wanted:
            layers[name] = F.empty_layer("not implemented yet")

    return {
        "schema": 1,
        "generated_at": datetime.datetime.now(datetime.timezone.utc)
                        .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "repo": repo_facts(repo_root),
        "config": cfg,
        "tools": {},
        "layers": layers,
        "findings": all_findings,
        "summary": F.build_summary(layers["size"], inventory, cfg),
    }


def main(argv=None):
    args = parse_args(argv)
    try:
        doc = build_document(args)
    except (C.ConfigError, ValueError) as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2
    text = json.dumps(doc, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
