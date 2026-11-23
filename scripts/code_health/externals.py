"""Optional tools: probe, provision, invoke, and degrade honestly when absent.

Nothing here is allowed to raise into the document. Every function returns a
fragment carrying its own status and, when not measured, a reason a human can
act on.

No `pip install --user`: PEP 668 rejects it on a Homebrew Python. The third
fallback is a plugin-owned venv under XDG_DATA_HOME.
"""

import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

# The pinned versions are the FALLBACK. `measure.py --update-tools` resolves the
# latest stable release from the registry and installs exactly that; these
# constants are what a run uses when the registry cannot be reached and the
# tool is not installed yet. A new version can change exit codes or output
# under the plugin: vulture 2.x moved "dead code found" from exit 1 to exit 3,
# and the dead-code layer silently stopped reporting anything -- which is why
# the running version is recorded in the `tools` block and in the history line.
LIZARD_VERSION = "1.23.0"
VULTURE_VERSION = "2.16"
REGISTRY_TIMEOUT = 15

# Chunked so a large repository does not exceed the OS argv limit; same
# size and same reason as sizes.py.
_ARG_CHUNK = 400

_VENV = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) \
    / "code-health" / "venv"

TOOLS = {
    "lizard": {
        "kind": "python", "pkg": "lizard", "pin": LIZARD_VERSION,
        "probe": ["lizard", "--version"],
    },
    "vulture": {
        "kind": "python", "pkg": "vulture", "pin": VULTURE_VERSION,
        "probe": ["vulture", "--version"],
    },
    "pre-commit": {
        "kind": "python", "pkg": "pre-commit", "pin": None,
        "probe": ["pre-commit", "--version"],
    },
    "jscpd": {"kind": "npx", "pkg": "jscpd", "pin": "5.4.1", "probe": ["--version"]},
    "madge": {"kind": "npx", "pkg": "madge", "pin": "8.0.0", "probe": ["--version"]},
    "knip": {"kind": "npx", "pkg": "knip", "pin": "6", "probe": ["--version"]},
}


def _spec(name, version=None):
    """Package spec for `version` (default: the pinned fallback)."""
    tool = TOOLS[name]
    version = version or tool["pin"]
    if tool["kind"] == "npx":
        return "%s@%s" % (tool["pkg"], version)
    return "%s==%s" % (tool["pkg"], version) if version else tool["pkg"]


for _name, _tool in TOOLS.items():
    _tool["package"] = _spec(_name)


def install_commands(name, version=None):
    """Install argv list, in the order the channels are tried. With `version`
    the install is forced to exactly that version; without, the pinned
    fallback is installed the way it always was."""
    tool = TOOLS[name]
    spec = _spec(name, version)
    if tool["kind"] == "npx":
        return [["npx", "-y", spec] + tool["probe"]]
    force = ["--force"] if version else []
    return [
        ["uv", "tool", "install"] + force + [spec],
        ["pipx", "install"] + force + [spec],
        [sys.executable, "-m", "venv", str(_VENV)],
    ]


def _absent(reason):
    return {"available": False, "version": None, "reason": reason,
            "installed_by_us": False}


def _version_line(proc):
    """Last non-empty output line. npx prefixes its own notices; the tool's own
    `--version` output is what comes last."""
    text = (getattr(proc, "stdout", "") or "").strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else None


def _npx_argv(name, tool_state):
    """Prefer a binary probe() already resolved on PATH; otherwise go through
    npx with the spec the survey settled on (the resolved latest version after
    `--update-tools`, the pinned fallback otherwise)."""
    binary = (tool_state or {}).get("bin")
    spec = (tool_state or {}).get("spec") or TOOLS[name]["package"]
    return [binary] if binary else ["npx", "-y", spec]


def _run(argv, timeout=120, cwd=None):
    try:
        return subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, cwd=cwd)
    except (OSError, subprocess.SubprocessError):
        return None


def _run_to_file(argv, timeout=120, cwd=None):
    """Like `_run`, with stdout read from a temp file instead of a pipe.

    Node tools exit before a pipe drains: on macOS madge's JSON stopped at
    65536 bytes with exit code 0. A file receives the whole output.
    """
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as out:
        try:
            proc = subprocess.run(argv, stdout=out, stderr=subprocess.PIPE,
                                  text=True, timeout=timeout, cwd=cwd)
        except (OSError, subprocess.SubprocessError):
            return None
        out.seek(0)
        proc.stdout = out.read()
        return proc


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
    on_path = shutil.which(name)
    if on_path:
        out = _run([on_path] + spec["probe"], timeout=60)
        if out is not None and out.returncode == 0:
            return {"available": True, "version": _version_line(out),
                    "reason": None, "installed_by_us": False, "bin": on_path}
    out = _run(["npx", "--no-install", name] + spec["probe"], timeout=60)
    if out is None or out.returncode != 0:
        return _absent("%s is not on PATH and `npx --no-install` cannot resolve it; "
                       "needs `npx -y %s` and network" % (name, spec["package"]))
    return {"available": True, "version": _version_line(out),
            "reason": None, "installed_by_us": False}


def _venv_bin(name):
    candidate = _VENV / "bin" / name
    return str(candidate) if candidate.exists() else None


def provision(name, version=None):
    spec = TOOLS.get(name)
    if spec is None:
        return _absent("unknown tool %r" % name)
    commands = install_commands(name, version)
    for argv in commands:
        if not shutil.which(argv[0]) and argv[0] != sys.executable:
            continue
        out = _run(argv, timeout=600)
        if out is None or out.returncode != 0:
            continue
        if argv[0] == sys.executable:
            pip = _VENV / "bin" / "pip"
            if _run([str(pip), "install", _spec(name, version)], timeout=600) is None:
                continue
        elif spec["kind"] == "npx":
            # `npx -y <pkg>` populates npx's OWN cache, keyed by the package
            # spec; `npx --no-install <name>` does not consult that cache and
            # does not consult PATH either. Re-probing here therefore reported
            # the tool that had just installed successfully as missing, and the
            # jscpd/madge layers came back `partial` on a machine where both
            # tools run fine. The install command already ran the probe
            # arguments, so its own output is the evidence.
            return {"available": True, "version": _version_line(out),
                    "reason": None, "installed_by_us": True,
                    "spec": _spec(name, version)}
        state = probe(name)
        if state["available"]:
            state["installed_by_us"] = True
            return state
    return _absent("could not install %s (tried: %s)"
                   % (name, "; ".join(" ".join(a) for a in commands)))


_STABLE = re.compile(r"^\d+(?:\.\d+){0,3}$")
_VERSION_IN_TEXT = re.compile(r"\d+(?:\.\d+)+")


def _http_json(url, timeout=REGISTRY_TIMEOUT):
    request = urllib.request.Request(url, headers={"User-Agent": "code-health",
                                                   "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def latest_version(name):
    """`(version, None)` for the newest stable release in the tool's registry,
    or `(None, one-line reason)`. PyPI's `info.version` and npm's `latest`
    dist-tag are both stable by construction; anything that does not look like
    a plain release number is refused anyway."""
    tool = TOOLS.get(name)
    if tool is None:
        return None, "unknown tool %r" % name
    if tool["kind"] == "npx":
        url, path = "https://registry.npmjs.org/%s/latest" % tool["pkg"], ("version",)
    else:
        url, path = "https://pypi.org/pypi/%s/json" % tool["pkg"], ("info", "version")
    try:
        data = _http_json(url, timeout=REGISTRY_TIMEOUT)
        for key in path:
            data = data[key]
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as exc:
        return None, ("registry unreachable or unreadable (%s): %s"
                      % (url, " ".join(str(exc).split())[:120]))
    if not isinstance(data, str) or not _STABLE.match(data):
        return None, "registry latest %r is not a stable release" % (data,)
    return data, None


def _same_version(a, b):
    def number(text):
        found = _VERSION_IN_TEXT.search(text or "")
        return found.group(0) if found else (text or "").strip().lstrip("v")
    return bool(a) and bool(b) and number(a) == number(b)


def update_tool(name):
    """Install the latest stable release of one tool, or leave it as it was.

    Never raises and never makes a working tool worse: a failed lookup or a
    failed install keeps what is installed and leaves a one-line reason in
    `update_error`. A tool that is missing falls back to the pinned version.
    """
    before = probe(name)
    previous = before["version"] if before["available"] else None
    latest, error = latest_version(name)
    state = before
    if latest:
        if not (before["available"] and _same_version(previous, latest)):
            installed = provision(name, latest)
            if installed["available"]:
                state = installed
                if not _same_version(state["version"], latest):
                    error = ("installed %s but the tool on PATH reports %s"
                             % (_spec(name, latest), state["version"]))
            else:
                error = "could not install %s: %s" % (_spec(name, latest),
                                                      installed["reason"])
                if not before["available"]:
                    state = provision(name)
        if TOOLS[name]["kind"] == "npx" and state.get("available") \
                and "bin" not in state and _same_version(state.get("version"), latest):
            state = dict(state, spec=_spec(name, latest))
    elif not before["available"]:
        state = provision(name)
    state = dict(state)
    state["latest"] = latest
    state["previous_version"] = previous
    state["updated"] = bool(previous and state.get("available")
                            and not _same_version(previous, state.get("version")))
    state["update_error"] = error
    return state


_VERSION_KEYS = {"latest": None, "updated": False, "previous_version": None,
                 "update_error": None}


def survey(names, install_missing=False, update=False):
    """Probe each tool. `install_missing` installs what is absent (pinned
    version); `update` also resolves the latest stable release and moves every
    tool to it, falling back to the installed or pinned one on any failure."""
    block = {}
    for name in names:
        if update and name in TOOLS:
            state = update_tool(name)
        else:
            state = probe(name)
            if not state["available"] and install_missing:
                state = provision(name)
        for key, default in _VERSION_KEYS.items():
            state.setdefault(key, default)
        block[name] = state
    return block


def run_jscpd(repo_root, cfg, tool_state, measured=None, code=None):
    """Exact clones between source files, at least one of them in `measured`.

    jscpd scans the whole repository; its report is narrowed here. Without
    that, JSON, YAML and Markdown clones made up most of the findings, and
    `measure.py --files` did not narrow the result at all. `code` is every
    source file of the repository (defaults to `measured`); `None` for
    `measured` keeps the unfiltered report, for callers that pass nothing.
    """
    if not tool_state.get("available"):
        return {"status": "not_measured",
                "reason": "jscpd unavailable: %s" % tool_state.get("reason"),
                "clones": []}
    out_dir = tempfile.mkdtemp(prefix="code-health-jscpd-")
    try:
        proc = _run(_npx_argv("jscpd", tool_state) +
                    ["--min-tokens", str(cfg["near_duplicate"]["min_tokens"]),
                     "--reporters", "json", "--output", out_dir, "--silent",
                     str(repo_root)], timeout=900)
        report = Path(out_dir) / "jscpd-report.json"
        if proc is None or not report.exists():
            return {"status": "not_measured", "reason": "jscpd produced no report",
                    "clones": []}
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
        except ValueError as exc:
            return {"status": "not_measured",
                    "reason": "jscpd report unreadable: %s" % exc, "clones": []}
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
    clones = data.get("duplicates", [])
    if measured is not None:
        measured = set(measured)
        code = set(code) if code is not None else measured
        clones = [c for c in clones if _clone_in(c, measured, code)]
    return {"status": "measured", "clones": clones}


def _clone_in(clone, measured, code):
    names = [(clone.get(side) or {}).get("name") for side in ("firstFile", "secondFile")]
    return all(n in code for n in names) and any(n in measured for n in names)


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
    #
    # madge is given the JS/TS FILE LIST, not the repository root: `relpaths`
    # used to be read for the emptiness check above and then ignored, so
    # `measure.py --files a.js` still walked the whole repository and the
    # restriction the caller asked for silently did not apply. Chunked for
    # the same argv reason as sizes.py; madge follows each entry point's
    # requires, so the union of the chunk graphs is the same graph.
    graph = {}
    for start in range(0, len(js), _ARG_CHUNK):
        proc = _run_to_file(_npx_argv("madge", tool_state) + ["--json"]
                    + js[start:start + _ARG_CHUNK], timeout=600, cwd=str(repo_root))
        if proc is None or proc.returncode != 0:
            return {"status": "not_measured", "reason": "madge failed",
                    "graph": {}, "cycles": []}
        try:
            chunk = json.loads(proc.stdout)
        except ValueError as exc:
            return {"status": "not_measured",
                    "reason": "madge output unreadable: %s" % exc,
                    "graph": {}, "cycles": []}
        for module, targets in chunk.items():
            graph.setdefault(module, [])
            graph[module] = sorted(set(graph[module]) | set(targets))
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
        # vulture 2.16: 0 -- clean, 3 -- dead code found, 1 -- a file could
        # not be parsed and nothing else was found, 2 -- bad arguments. A
        # file that cannot be parsed is named on stderr whatever the exit
        # code, so stderr marks the result partial. Verified with the real
        # tool; tests/test_externals.py runs it.
        if proc is None:
            reasons.append("vulture did not run")
        elif proc.returncode not in (0, 1, 3):
            reasons.append("vulture failed (exit %d): %s"
                           % (proc.returncode, (proc.stderr or "").strip()[:200]))
        else:
            if (proc.stderr or "").strip():
                reasons.append("vulture could not analyze every file: %s"
                               % (proc.stderr or "").strip()[:200])
            measured.append("python")
            parsed = _parse_vulture(proc.stdout)
            findings.extend(parsed)
            # The latest vulture is installed, not the one this parser was
            # verified against. A line it cannot read must not turn into a
            # silent zero: the layer says it was not fully parsed.
            lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
            if len(parsed) < len(lines):
                reasons.append("vulture output could not be parsed: %d of %d lines"
                               % (len(lines) - len(parsed), len(lines)))
    elif py:
        reasons.append("vulture unavailable: %s" % vulture.get("reason"))

    if js:
        # knip was invoked here and its stdout was never read -- `measured`
        # was appended on the return code alone, which is precisely the
        # failure mode already fixed for vulture above: a confident "no dead
        # code in JS/TS" produced by parsing nothing. This plugin has no
        # verified parser for knip's JSON reporter, so JS/TS dead code is
        # reported as NOT measured, with the reason saying so, and knip is
        # not run at all rather than burning a 900-second npx subprocess for
        # a result nobody reads. Writing an unverified parser would restore
        # the same lie one level down.
        reasons.append("JS/TS dead code is not measured: knip's JSON output "
                       "is not parsed by this plugin")

    if not measured:
        return {"status": "not_measured",
                "reason": "; ".join(reasons) or "no supported sources",
                "symbols": []}
    return {"status": "measured" if not reasons else "partial",
            "reason": "; ".join(reasons) or None,
            "languages_measured": measured, "symbols": findings}


# `path/to/mod.py:12: unused function 'compute' (60% confidence)`
_VULTURE_SYMBOL = re.compile(r"unused\s+[\w ]+?\s+'([^']+)'")
_VULTURE_CONFIDENCE = re.compile(r"\((\d+)% confidence\)")


def _parse_vulture(text):
    out = []
    for line in (text or "").splitlines():
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        message = parts[2].strip()
        name = _VULTURE_SYMBOL.search(message)
        confidence = _VULTURE_CONFIDENCE.search(message)
        out.append({
            "file": parts[0], "line": parts[1], "message": message,
            "tool": "vulture",
            # The symbol name is what a stable `dead_symbol:<file>:<name>`
            # finding id is built from; the line number moves with any edit
            # above it. `None` when vulture phrases a message some other way.
            "name": name.group(1) if name else None,
            "confidence": int(confidence.group(1)) if confidence else None,
        })
    return out
