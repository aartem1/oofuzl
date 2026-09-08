"""Optional tools: probe, provision, invoke, and degrade honestly when absent.

Nothing here is allowed to raise into the document. Every function returns a
fragment carrying its own status and, when not measured, a reason a human can
act on.

No `pip install --user`: PEP 668 rejects it on a Homebrew Python. The third
fallback is a plugin-owned venv under XDG_DATA_HOME.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

LIZARD_VERSION = "1.23.0"

_VENV = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) \
    / "code-health" / "venv"

TOOLS = {
    "lizard": {
        "kind": "python", "package": "lizard==%s" % LIZARD_VERSION,
        "probe": ["lizard", "--version"],
    },
    "vulture": {
        "kind": "python", "package": "vulture", "probe": ["vulture", "--version"],
    },
    "pre-commit": {
        "kind": "python", "package": "pre-commit", "probe": ["pre-commit", "--version"],
    },
    "jscpd": {"kind": "npx", "package": "jscpd@5", "probe": ["--version"]},
    "madge": {"kind": "npx", "package": "madge@8", "probe": ["--version"]},
    "knip": {"kind": "npx", "package": "knip@6", "probe": ["--version"]},
}

for _name, _spec in TOOLS.items():
    if _spec["kind"] == "python":
        _spec["install"] = [
            ["uv", "tool", "install", _spec["package"]],
            ["pipx", "install", _spec["package"]],
            [sys.executable, "-m", "venv", str(_VENV)],
        ]
    else:
        _spec["install"] = [["npx", "-y", _spec["package"]] + _spec["probe"]]


def _absent(reason):
    return {"available": False, "version": None, "reason": reason,
            "installed_by_us": False}


def _run(argv, timeout=120, cwd=None):
    try:
        return subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, cwd=cwd)
    except (OSError, subprocess.SubprocessError):
        return None


def probe(name):
    spec = TOOLS.get(name)
    if spec is None:
        return _absent("unknown tool %r" % name)
    if spec["kind"] == "python":
        found = shutil.which(name) or _venv_bin(name)
        if not found:
            return _absent("%s is not installed" % name)
        out = _run([found] + spec["probe"][1:])
        if out is None or out.returncode != 0:
            return _absent("%s did not respond to %s" % (name, spec["probe"][1:]))
        return {"available": True, "version": (out.stdout or out.stderr).strip().split()[-1],
                "reason": None, "installed_by_us": False, "bin": found}
    out = _run(["npx", "--no-install", name] + spec["probe"], timeout=60)
    if out is None or out.returncode != 0:
        return _absent("%s is not in the npx cache; needs `npx -y %s` and network"
                       % (name, spec["package"]))
    lines = (out.stdout or "").strip().splitlines()
    return {"available": True, "version": lines[-1] if lines else None,
            "reason": None, "installed_by_us": False}


def _venv_bin(name):
    candidate = _VENV / "bin" / name
    return str(candidate) if candidate.exists() else None


def provision(name):
    spec = TOOLS.get(name)
    if spec is None:
        return _absent("unknown tool %r" % name)
    for argv in spec["install"]:
        if not shutil.which(argv[0]) and argv[0] != sys.executable:
            continue
        out = _run(argv, timeout=600)
        if out is None or out.returncode != 0:
            continue
        if argv[0] == sys.executable:
            pip = _VENV / "bin" / "pip"
            if _run([str(pip), "install", spec["package"]], timeout=600) is None:
                continue
        state = probe(name)
        if state["available"]:
            state["installed_by_us"] = True
            return state
    return _absent("could not install %s (tried: %s)"
                   % (name, "; ".join(" ".join(a) for a in spec["install"])))


def survey(names, install_missing=False):
    block = {}
    for name in names:
        state = probe(name)
        if not state["available"] and install_missing:
            state = provision(name)
        block[name] = state
    return block


def run_jscpd(repo_root, cfg, tool_state):
    if not tool_state.get("available"):
        return {"status": "not_measured",
                "reason": "jscpd unavailable: %s" % tool_state.get("reason"),
                "clones": []}
    out_dir = tempfile.mkdtemp(prefix="code-health-jscpd-")
    proc = _run(["npx", "-y", TOOLS["jscpd"]["package"],
                 "--min-tokens", str(cfg["near_duplicate"]["min_tokens"]),
                 "--reporters", "json", "--output", out_dir, "--silent",
                 str(repo_root)], timeout=900)
    report = Path(out_dir) / "jscpd-report.json"
    if proc is None or not report.exists():
        return {"status": "not_measured", "reason": "jscpd produced no report",
                "clones": []}
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except ValueError as exc:
        return {"status": "not_measured", "reason": "jscpd report unreadable: %s" % exc,
                "clones": []}
    return {"status": "measured", "clones": data.get("duplicates", [])}


def run_madge(repo_root, relpaths, tool_state):
    js = [p for p in relpaths if p.endswith((".js", ".jsx", ".mjs", ".cjs",
                                             ".ts", ".tsx"))]
    if not js:
        return {"status": "not_measured", "reason": "no JS/TS sources",
                "graph": {}, "cycles": []}
    if not tool_state.get("available"):
        return {"status": "not_measured",
                "reason": "madge unavailable: %s" % tool_state.get("reason"),
                "graph": {}, "cycles": []}
    # `--circular` REPLACES the dependency map in the output, so it cannot give
    # both. Ask for the map and derive the cycles from it.
    proc = _run(["npx", "-y", TOOLS["madge"]["package"], "--json", str(repo_root)],
                timeout=600)
    if proc is None or proc.returncode != 0:
        return {"status": "not_measured", "reason": "madge failed",
                "graph": {}, "cycles": []}
    try:
        graph = json.loads(proc.stdout)
    except ValueError as exc:
        return {"status": "not_measured", "reason": "madge output unreadable: %s" % exc,
                "graph": {}, "cycles": []}
    from . import pyast
    return {"status": "measured", "graph": graph,
            "cycles": pyast.find_cycles({k: list(v) for k, v in graph.items()})}


def run_dead(repo_root, inventory, tool_states):
    py = [p for p in inventory.source if p.endswith(".py")]
    js = [p for p in inventory.source
          if p.endswith((".js", ".jsx", ".ts", ".tsx"))]
    findings, measured, reasons = [], [], []

    vulture = tool_states.get("vulture", {})
    if py and vulture.get("available"):
        proc = _run([vulture.get("bin", "vulture"), "--min-confidence", "80"] + py,
                    cwd=str(repo_root), timeout=600)
        # vulture exits 1 for BOTH "found dead code" and "error", so the return
        # code alone cannot tell them apart -- stderr is the discriminator.
        # Verified: clean -> rc 0, empty stderr; dead code found -> rc 1, empty
        # stderr; syntax error or missing file -> rc 1 WITH stderr.
        # Without this, a failing tool reports `measured` with zero symbols,
        # which is indistinguishable from "ran clean, found nothing" -- the one
        # thing every other layer in this design refuses to do.
        if proc is None:
            reasons.append("vulture did not run")
        elif proc.returncode not in (0, 1):
            reasons.append("vulture failed (exit %d): %s"
                           % (proc.returncode, (proc.stderr or "").strip()[:200]))
        else:
            if (proc.stderr or "").strip():
                reasons.append("vulture could not analyze every file: %s"
                               % (proc.stderr or "").strip()[:200])
            measured.append("python")
            findings.extend(_parse_vulture(proc.stdout))
    elif py:
        reasons.append("vulture unavailable: %s" % vulture.get("reason"))

    knip = tool_states.get("knip", {})
    if js and knip.get("available"):
        proc = _run(["npx", "-y", TOOLS["knip"]["package"], "--reporter", "json"],
                    timeout=900, cwd=str(repo_root))
        if proc is not None and proc.returncode in (0, 1):
            measured.append("javascript")
    elif js:
        reasons.append("knip unavailable: %s" % knip.get("reason"))

    if not measured:
        return {"status": "not_measured",
                "reason": "; ".join(reasons) or "no supported sources",
                "symbols": []}
    return {"status": "measured" if not reasons else "partial",
            "reason": "; ".join(reasons) or None,
            "languages_measured": measured, "symbols": findings}


def _parse_vulture(text):
    out = []
    for line in (text or "").splitlines():
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        out.append({"file": parts[0], "line": parts[1], "message": parts[2].strip()})
    return out
