"""Python structure via stdlib ast: import graph, cycles, parameter names, symbols.

Exact and dependency-free. Sonar Community Build has no import graph at all,
and madge only covers JS/TS.
"""

import ast
from pathlib import Path


def module_name(relpath):
    parts = relpath[:-3].split("/") if relpath.endswith(".py") else relpath.split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _resolve_relative(current_module, is_package, level, target):
    """Resolve `from ...target import x` the way Python does.

    The base is the importing module's PACKAGE: for `pkg/a.py` that is `pkg`,
    for `pkg/__init__.py` it is `pkg` itself. Level 1 means that package,
    level 2 its parent. Getting the __init__ case wrong silently drops every
    relative import inside every package root.
    """
    parts = current_module.split(".") if current_module else []
    if not is_package:
        parts = parts[:-1]
    if level > 1:
        parts = parts[:len(parts) - (level - 1)]
    if target:
        parts = parts + target.split(".")
    return ".".join(p for p in parts if p)


def _candidates(name):
    """A module and every package prefix of it, longest first."""
    parts = name.split(".")
    return [".".join(parts[:i]) for i in range(len(parts), 0, -1)]


def analyze(repo_root, relpaths):
    repo_root = Path(repo_root)
    python_files = [p for p in relpaths if p.endswith(".py")]
    known = {module_name(p): p for p in python_files}

    graph = {name: set() for name in known}
    unresolved = 0
    params, symbols, errors = [], [], []

    for relpath in python_files:
        current = module_name(relpath)
        is_package = relpath.endswith("/__init__.py") or relpath == "__init__.py"
        try:
            source = (repo_root / relpath).read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
        except (OSError, SyntaxError, ValueError) as exc:
            errors.append({"file": relpath, "error": str(exc)})
            graph.pop(current, None)
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    unresolved += _add_edge(graph, known, current, alias.name)
            elif isinstance(node, ast.ImportFrom):
                target = _resolve_relative(current, is_package, node.level or 0,
                                           node.module or "")
                if node.level:
                    # `from .b import x` may name a module or a symbol in b.
                    hit = _add_edge(graph, known, current, target)
                    for alias in node.names:
                        _add_edge(graph, known, current, "%s.%s" % (target, alias.name))
                    unresolved += hit
                else:
                    unresolved += _add_edge(graph, known, current, node.module or "")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                params.append({"file": relpath, "function": node.name,
                               "names": _param_names(node)})
                symbols.append({"name": node.name, "file": relpath,
                                "kind": "function",
                                "tokens": _node_tokens(source, node)})
            elif isinstance(node, ast.ClassDef):
                symbols.append({"name": node.name, "file": relpath, "kind": "class",
                                "tokens": _node_tokens(source, node)})

    return {
        "graph": {name: sorted(edges) for name, edges in graph.items()},
        "unresolved": unresolved,
        "params": params,
        "symbols": symbols,
        "errors": errors,
    }


def _add_edge(graph, known, current, target):
    """Return 1 when the import points outside the repository, else 0."""
    if not target:
        return 0
    for candidate in _candidates(target):
        if candidate in known and candidate != current:
            graph.setdefault(current, set()).add(candidate)
            return 0
        if candidate in known:
            return 0
    return 1


def _param_names(node):
    a = node.args
    names = [arg.arg for arg in list(getattr(a, "posonlyargs", [])) + list(a.args)]
    if a.vararg:
        names.append(a.vararg.arg)
    names.extend(arg.arg for arg in a.kwonlyargs)
    if a.kwarg:
        names.append(a.kwarg.arg)
    return names


def _node_tokens(source, node):
    end = getattr(node, "end_lineno", node.lineno)
    lines = source.splitlines()[node.lineno - 1:end]
    return len("\n".join(lines)) // 4


def find_cycles(graph):
    """Tarjan strongly connected components of size >= 2, plus self-loops."""
    index = {}
    low = {}
    stack = []
    on_stack = set()
    counter = [0]
    result = []

    def strongconnect(node):
        index[node] = low[node] = counter[0]
        counter[0] += 1
        stack.append(node)
        on_stack.add(node)
        for peer in graph.get(node, ()):
            if peer not in graph:
                continue
            if peer not in index:
                strongconnect(peer)
                low[node] = min(low[node], low[peer])
            elif peer in on_stack:
                low[node] = min(low[node], index[peer])
        if low[node] == index[node]:
            component = []
            while True:
                top = stack.pop()
                on_stack.discard(top)
                component.append(top)
                if top == node:
                    break
            if len(component) > 1 or node in graph.get(node, ()):
                result.append(sorted(component))

    import sys
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(limit, len(graph) * 4 + 1000))
    try:
        for node in sorted(graph):
            if node not in index:
                strongconnect(node)
    finally:
        sys.setrecursionlimit(limit)
    return sorted(result)


def measure_imports(repo_root, inventory, cfg):
    relpaths = list(inventory.source) + list(inventory.tests)
    if not any(p.endswith(".py") for p in relpaths):
        return {"status": "not_measured", "languages_measured": [],
                "reason": "no Python sources; JS/TS needs madge (see externals)",
                "cycles": [], "degree": []}

    out = analyze(repo_root, relpaths)
    graph = out["graph"]
    fan_in = {name: 0 for name in graph}
    for edges in graph.values():
        for target in edges:
            if target in fan_in:
                fan_in[target] += 1

    degree = [{"module": name, "fan_out": len(graph[name]), "fan_in": fan_in[name]}
              for name in sorted(graph)]
    return {
        "status": "partial" if out["errors"] else "measured",
        "languages_measured": ["python"],
        "reason": ("%d file(s) failed to parse" % len(out["errors"])) if out["errors"] else None,
        "cycles": find_cycles(graph),
        "degree": degree,
        "unresolved_imports": out["unresolved"],
        "params": out["params"],
        "symbols": out["symbols"],
        "errors": out["errors"],
    }
