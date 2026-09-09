"""Comments that carry no information beyond the code next to them.

Three detectors, two of which count toward the metric. `commented_out` ports
eradicate's algorithm and ends in a real `compile()`; `trivial_doc` ports
Steidl's coherence coefficient (ICPC 2013), whose 0.5 threshold was validated
by a 16-developer survey. `short_inline` is measured at 0.70 precision and is
therefore reported in evidence only -- summing three classes would give the
whole metric the precision of its weakest member.

An immunity pass runs before every detector. Steidl and Pascarella independently
measured that licence headers, task tags and tool directives make up most of a
repository's comment volume; without the pass this module measures copyright.
"""

import ast
import io
import tokenize
from collections import namedtuple

# 1-based lines. `text` has comment markers stripped; consecutive own-line
# comments are merged into one record, because every threshold below is
# defined over a comment as a unit, not over a physical line.
Comment = namedtuple("Comment", "line end_line text inline")


def extract_python(text):
    """Comments and docstrings of one Python source, or None if it does not parse."""
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return None
    lines = text.splitlines()
    raw = []
    for tok in toks:
        if tok.type != tokenize.COMMENT:
            continue
        row, col = tok.start
        before = lines[row - 1][:col] if row - 1 < len(lines) else ""
        raw.append(Comment(line=row, end_line=row,
                           text=tok.string.lstrip("#").strip(),
                           inline=bool(before.strip())))
    return _merge_runs(raw) + _python_docstrings(text)


def _merge_runs(items):
    """Fold each run of own-line comments on consecutive lines into one record."""
    out = []
    for item in items:
        prev = out[-1] if out else None
        if (prev is not None and not prev.inline and not item.inline
                and item.line == prev.end_line + 1):
            out[-1] = prev._replace(end_line=item.end_line,
                                    text=prev.text + "\n" + item.text)
        else:
            out.append(item)
    return out


_DOC_NODES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _python_docstrings(text):
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, _DOC_NODES):
            continue
        if ast.get_docstring(node) is None:
            continue
        expr = node.body[0]
        out.append(Comment(line=expr.lineno, end_line=expr.end_lineno,
                           text=ast.get_docstring(node).strip(), inline=False))
    return sorted(out, key=lambda c: c.line)
