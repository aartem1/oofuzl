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
        error, file_unresolved, file_params, file_symbols = _analyze_file(
            repo_root, relpath, known, graph)
        if error is not None:
            errors.append(error)
            graph.pop(module_name(relpath), None)
            continue
        unresolved += file_unresolved
        params.extend(file_params)
        symbols.extend(file_symbols)

    return {
        "graph": {name: sorted(edges) for name, edges in graph.items()},
        "unresolved": unresolved,
        "params": params,
        "symbols": symbols,
        "errors": errors,
    }


def _analyze_file(repo_root, relpath, known, graph):
    """Parse one file and collect its import edges, params, and symbols.

    Returns `(error, unresolved, params, symbols)`. On a parse failure,
    `error` is the `{"file", "error"}` dict for the caller to record, and the
    other three come back empty -- the caller is also responsible for
    popping this file's module out of `graph`, since this helper never
    touches `graph` on the failure path.
    """
    current = module_name(relpath)
    is_package = relpath.endswith("/__init__.py") or relpath == "__init__.py"
    try:
        source = (repo_root / relpath).read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except (OSError, SyntaxError, ValueError) as exc:
        return {"file": relpath, "error": str(exc)}, 0, [], []

    unresolved = 0
    params, symbols = [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                unresolved += _add_edge(graph, known, current, alias.name)
        elif isinstance(node, ast.ImportFrom):
            unresolved += _add_import_from(graph, known, current, is_package, node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            params.append({"file": relpath, "function": node.name,
                           "names": _param_names(node)})
            symbols.append({"name": node.name, "file": relpath, "kind": "function",
                            "tokens": _node_tokens(source, node)})
        elif isinstance(node, ast.ClassDef):
            symbols.append({"name": node.name, "file": relpath, "kind": "class",
                            "tokens": _node_tokens(source, node)})
    return None, unresolved, params, symbols


def _add_import_from(graph, known, current, is_package, node):
    """Resolve and record one `from X import ...` statement's edge(s).

    Returns 1 when it points outside the repository (unresolved), else 0 --
    same contract as `_add_edge`.
    """
    target = _resolve_relative(current, is_package, node.level or 0, node.module or "")
    if not node.level:
        return _add_edge(graph, known, current, node.module or "")
    # `from .b import x` may name a module or a symbol in b.
    hit = _add_edge(graph, known, current, target)
    for alias in node.names:
        _add_edge(graph, known, current, "%s.%s" % (target, alias.name))
    return hit


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


##
## Exact nesting depth, from the same AST -- no lizard involved.
##
## `lizard -ENS`'s own nesting counter keys its reset on a closing brace
## (`bracket = '}'`, from `lizard_ext/lizardnd.py`'s per-reader defaults).
## Every brace/semicolon language reader tested resets correctly on it.
## Python's indentation-based reader never emits that token, so the counter
## never resets: it climbs across every `if`/`for`/`while`/... in a function
## and keeps climbing into every function that follows in the SAME lizard
## invocation. Verified directly: 5 sequential (unnested) `if`s report
## nesting 5 instead of 1, and three copies of that one function in a single
## `lizard --csv a.py a.py a.py` report 5, 10, 15 -- a monotonically
## increasing counter, not a per-function measurement. `sizes.py` uses
## `measure_nesting` below to replace lizard's Python column with this one.
##

_NESTING_STMTS = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try)
if hasattr(ast, "TryStar"):     # 3.11+, `except*`
    _NESTING_STMTS = _NESTING_STMTS + (ast.TryStar,)
if hasattr(ast, "Match"):       # 3.10+, `match`/`case`
    _NESTING_STMTS = _NESTING_STMTS + (ast.Match,)


def _if_depth(node, depth):
    """One `if`/`elif`/`else` chain is one nesting level, not one per branch.

    `elif` has no AST node of its own: `if a: ..\nelif b: ..` and
    `if a: ..\nelse:\n    if b: ..` produce the identical
    `If(orelse=[If(...)])` shape, so the two cannot be told apart from the
    tree alone. Both are treated as continuing the same chain -- staying at
    `depth`, not `depth + 1` -- matching how a human reads an
    if/elif/.../else ladder as a single decision. This is the same
    "else-if" exception `lizardnd.py` carves out for brace languages
    (`prev_was_else`), applied here at the AST level instead of the token
    level, and it means a genuine `else:` block with unrelated nested `if`
    written at the same indentation as an `elif` is indistinguishable from
    one and is treated the same way -- an inherent ambiguity of the
    grammar, not a gap in this pass.
    """
    deepest = _max_depth(node.body, depth + 1)
    orelse = node.orelse
    if len(orelse) == 1 and isinstance(orelse[0], ast.If):
        deepest = max(deepest, _if_depth(orelse[0], depth))
    elif orelse:
        deepest = max(deepest, _max_depth(orelse, depth + 1))
    return deepest


def _stmt_bodies(node):
    """Every statement list one nesting construct can hold, each at the
    SAME depth as its siblings: an `except`/`else`/`finally` clause is not
    an additional level beyond its `try`, and one `case` is not additional
    beyond its `match` -- matching the task's grouping of
    `try/except/finally` and `match/case` as single constructs."""
    if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
        bodies = [node.body]
        if node.orelse:
            bodies.append(node.orelse)
        return bodies
    if isinstance(node, ast.Try) or (hasattr(ast, "TryStar") and isinstance(node, ast.TryStar)):
        bodies = [node.body] + [handler.body for handler in node.handlers]
        if node.orelse:
            bodies.append(node.orelse)
        if node.finalbody:
            bodies.append(node.finalbody)
        return bodies
    if hasattr(ast, "Match") and isinstance(node, ast.Match):
        return [case.body for case in node.cases]
    return [node.body]     # With / AsyncWith


def _max_depth(stmts, depth):
    """Deepest nesting depth reached inside `stmts`, starting at `depth`.

    Does not descend into a nested function or class definition at all --
    each is walked separately by its own top-level call (see
    `measure_nesting`), starting back at depth 0. A nested function's depth
    is its own, never inherited from its parent's.
    """
    deepest = depth
    for stmt in stmts:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(stmt, ast.If):
            deepest = max(deepest, _if_depth(stmt, depth))
        elif isinstance(stmt, _NESTING_STMTS):
            for body in _stmt_bodies(stmt):
                deepest = max(deepest, _max_depth(body, depth + 1))
    return deepest


def max_nesting(node):
    """A function's own maximum nesting depth: `if`/`elif`/`else`, `for`,
    `while`, `with`, `try`/`except`/`finally`, `match`/`case`, each enclosing
    a statement, add one level. A function body at depth 0 containing one
    `if` has depth 1; a lone `if` sequence of any length that never nests
    also has depth 1 -- unlike lizard's Python reader, this does not count
    the number of `if` keywords."""
    return _max_depth(node.body, 0)


def measure_nesting(repo_root, relpaths):
    """Per-function exact nesting depth for every `.py` path in `relpaths`.

    Returns `(rows, errors)`. Each row is
    `{"file", "function", "start_line", "end_line", "nesting"}`; `start_line`
    is `node.lineno`, the `def`/`async def` line itself -- verified to be
    what lizard also reports as `start_line` for a decorated function
    (including a multi-line decorator), so it is the reliable half of the
    join key `sizes.py` uses against lizard's rows. `function` is the bare
    name ast gives a nested function or method (`wrapper`, not lizard's
    qualified `outer.wrapper`), so it is NOT part of that join key -- name
    qualification is exactly where the two disagree.

    Independent of `analyze`/the import graph on purpose: `sizes.py` needs
    this even when the `imports` layer was never requested, and should not
    pay for building a graph it does not use to get it.
    """
    repo_root = Path(repo_root)
    rows, errors = [], []
    for relpath in relpaths:
        if not relpath.endswith(".py"):
            continue
        try:
            source = (repo_root / relpath).read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
        except (OSError, SyntaxError, ValueError) as exc:
            errors.append({"file": relpath, "error": str(exc)})
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                rows.append({
                    "file": relpath,
                    "function": node.name,
                    "start_line": node.lineno,
                    "end_line": getattr(node, "end_lineno", node.lineno),
                    "nesting": max_nesting(node),
                })
    return rows, errors


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
