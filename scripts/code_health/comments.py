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
import re
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


# Extensions with a verified `//` + `/* */` reader. `.lua` (`--`), `.rb` (`#`
# plus `=begin`) and `.php` (three syntaxes, one of them `#`) are deliberately
# absent: each needs its own reader, and an unverified one would report a
# number nobody checked -- the defect this repository already refuses for
# lizard's nesting column.
BRACE_EXT = frozenset({
    ".c", ".h", ".cpp", ".cc", ".hpp", ".cs", ".java",
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
    ".go", ".kt", ".rs", ".swift", ".scala", ".m", ".mm",
})


# Two jobs, two functions: Directives say "this is metadata or tooling, not source
# code" and must gate every detector. Prose says "a human wrote informative text"
# and must gate only prose detectors. A `# return None` is a return statement, not
# documentation of null semantics — directives protect it, prose patterns do not.
_DIRECTIVE_PATTERNS = (
    r"\bnoqa\b|\btype:\s*ignore|\beslint-disable|\bpylint\b|\bpyright\b"
    r"|\bnosec\b|\bfmt:\s*(on|off)|\bisort:|\bNOSONAR\b|\bgo:generate\b"
    r"|\bruff:\s|\bmypy:",
    r"\bTODO\b|\bFIXME\b|\bXXX\b|\bHACK\b",
    r"\bcopyright\b|\blicen[sc]e\b|(?-i:\bSPDX\b)",
    r"#\d+|(?-i:\b[A-Z]{2,}-\d+\b)|https?://",
    r"^#?!/|-\*-\s*coding",
)
_DIRECTIVE_RE = re.compile("|".join("(?:%s)" % p for p in _DIRECTIVE_PATTERNS),
                           re.IGNORECASE | re.MULTILINE)

_PROSE_PATTERNS = (
    r"\bbecause\b|\bworkaround\b|\botherwise\b"
    r"|\bиначе\b|\bпотому что\b|\bпоэтому\b",
    r"\bms\b|\bmsec\b|\bsec\b|\bbytes?\b|\bUTC\b|\bepoch\b"
    r"|\b[01]-based\b|\bpx\b|\bhz\b",
    r"\binclusive\b|\bexclusive\b|\bвключительно\b",
    r"\bnull\b|(?-i:\bNone\b)|\bnil\b|\bundefined\b",
    r"\bcaller must\b|\bowns\b|\bfrees\b|\breleases\b|\bосвобожда",
)
_PROSE_RE = re.compile("|".join("(?:%s)" % p for p in _PROSE_PATTERNS),
                       re.IGNORECASE | re.MULTILINE)

# Steidl's second validated length rule: developers kept the comment in 10 of
# 10 examples at this length, with >=88% agreement in nine of them.
LONG_COMMENT_WORDS = 30

# eradicate v3.0.1's pipeline. The final step is a real parse, which is why
# this class is the most accurate of the three (P 0.89 / R 0.95, Steidl 2013);
# everything above it exists to stop prose from ever reaching `compile`.
_CODE_INDICATORS = ("(", ")", "[", "]", "{", "}", ":", "=", "%")
_CODE_WORDS = ("print", "return", "break", "continue", "import", "pass", "raise")
_MULTILINE_ASSIGN = re.compile(r"^\s*\w+\s*=.*[(\[{]$")
_BRACKETS_ONLY = re.compile(r"^[\s)\]}]+[,:]?$")
_DICT_ITEM = re.compile(r"""^\s*['"]\w+['"]\s*:.+[,{]\s*$""")
_BLOCK_KEYWORD = re.compile(r"^\s*(if|elif|else|for|while|with|try|except|finally|def|class|async)\b.*:\s*$")
_STATEMENT_KEYWORD = re.compile(r"^\s*(return|break|continue|yield|raise)\b.*")

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def words_of(text):
    """Alphabetic words of a comment, lowercased, after marker normalisation."""
    return [w.lower() for w in _WORD_RE.findall(text)]


def is_directive(text):
    """True when the comment is a tool directive, task tag, or metadata declaration."""
    return bool(_DIRECTIVE_RE.search(text))


def is_prose_immune(text):
    """True when the comment is long-form prose or documents informative values."""
    if len(words_of(text)) >= LONG_COMMENT_WORDS:
        return True
    return bool(_PROSE_RE.search(text))


def is_immune(text):
    """True when the comment must not be classified as noise (directives or prose)."""
    return is_directive(text) or is_prose_immune(text)


def is_commented_out(text):
    """True when the comment body parses or reads as Python source."""
    if is_directive(text):
        return False
    return any(_line_is_code(line) for line in text.splitlines())


def _line_is_code(line):
    line = line.strip()
    if not line:
        return False
    if not (any(ind in line for ind in _CODE_INDICATORS)
            or any(re.search(r"\b%s\b" % w, line) for w in _CODE_WORDS)):
        return False
    if (_BLOCK_KEYWORD.match(line) or _BRACKETS_ONLY.match(line)
            or _STATEMENT_KEYWORD.match(line)):
        return True
    if _MULTILINE_ASSIGN.match(line) or line.endswith("\\") or _DICT_ITEM.match(line):
        return True
    try:
        compile(line, "<comment>", "exec")
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        # MemoryError: CPython's parser raises this on stack overflow when
        # processing pathological input like a 50k-character unary operator run.
        # Catching it prevents a single malformed comment from crashing the audit.
        return False
    return True


def extract(text, ext):
    """Comments of one source file, or None when no reader is verified for `ext`."""
    if ext == ".py":
        return extract_python(text)
    if ext in BRACE_EXT:
        return extract_brace(text)
    return None


def extract_brace(text):
    """`//` and `/* */`, with string, char and template literals skipped."""
    out = []
    i, n, line = 0, len(text), 1
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch in "\"'`":
            i, line = _skip_literal(text, i, line)
            continue
        if ch == "/" and nxt == "/":
            start = i + 2
            end = text.find("\n", start)
            if end == -1:
                end = n
            out.append(Comment(line=line, end_line=line,
                               text=text[start:end].strip(),
                               inline=_has_code_before(text, i)))
            i = end
            continue
        if ch == "/" and nxt == "*":
            start = i + 2
            end = text.find("*/", start)
            body = text[start:end if end != -1 else n]
            out.append(Comment(line=line, end_line=line + body.count("\n"),
                               text=_strip_stars(body),
                               inline=_has_code_before(text, i)))
            line += body.count("\n")
            i = n if end == -1 else end + 2
            continue
        if ch == "\n":
            line += 1
        i += 1
    return _merge_runs(out)


def _skip_literal(text, i, line):
    """Advance past the literal opening at `i`; return the new (index, line)."""
    quote, n = text[i], len(text)
    i += 1
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "\n":
            line += 1
        if ch == quote:
            return i + 1, line
        i += 1
    return n, line


def _has_code_before(text, i):
    start = text.rfind("\n", 0, i) + 1
    return bool(text[start:i].strip())


def _strip_stars(body):
    kept = [ln.strip().lstrip("*").strip() for ln in body.splitlines()]
    return "\n".join(ln for ln in kept if ln)


# Steidl (ICPC 2013) section 4: a comment whose words are mostly the words of
# the identifier it sits on is trivial. The 0.5 threshold is the paper's, and
# it was validated by asking 16 developers about 30 examples -- in nine of ten
# trivial cases, over 80% agreed. Correctness of the automatic verdict: ~90%.
TRIVIAL_COHERENCE = 0.5

# A digit stays glued to the segment before it (`name2` in `name2Value`) --
# the only boundary the pattern below recognises there is lower/digit -> upper,
# which falls after the digit, not before it.
_SPLIT_RE = re.compile(
    r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def split_identifier(name):
    return [part.lower() for part in _SPLIT_RE.split(name) if part]


def _within_one_edit(a, b):
    """Levenshtein distance below 2, without building the DP table."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    if i == len(a):
        return True
    if len(a) == len(b):
        return a[i + 1:] == b[i + 1:]
    return a[i:] == b[i + 1:]


def coherence(text, identifier):
    """Share of the comment's words already present in the identifier's name."""
    words = words_of(text)
    if not words:
        return None
    parts = split_identifier(identifier)
    hit = sum(1 for w in words if any(_within_one_edit(w, p) for p in parts))
    return hit / len(words)


def declarations_python(text):
    """Line number of each class/function declaration mapped to its bare name."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return {}
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.lineno] = node.name
    return out


# Three declaration shapes cover the brace languages this module reads: a
# keyword form (`function f`, `class C`, `func F`, `fn f`, `struct S`, and a Go
# receiver method `func (c *Cache) Load()`), an arrow/lambda form bound to a
# name (`const load = async () => {`, `const load = function() {`), and the
# C/Java/C# typed form (`static int read_block(`, `static char *read_line(`).
# A bare `name(` is excluded on purpose -- that is a call site, and treating
# it as a declaration would attach every comment above a call to the callee's
# name.
_KEYWORD_DECL = re.compile(
    r"^[^\S\n]*(?:[\w@$]+[^\S\n]+)*?"
    r"(?:function|func|fn|class|struct|interface|type|enum|protocol)"
    r"[^\S\n]+(?:\([^)]*\)[^\S\n]+)?([A-Za-z_$][\w$]*)")
_ARROW_DECL = re.compile(
    r"^[^\S\n]*(?:export[^\S\n]+)?(?:const|let|var)[^\S\n]+([A-Za-z_$][\w$]*)"
    r"[^\S\n]*=[^\S\n]*(?:async[^\S\n]+)?(?:\([^)]*\)[^\S\n]*=>|function\b)")
_TYPED_DECL = re.compile(
    r"^[^\S\n]*(?:[\w.<>\[\]*&$]+[^\S\n]+)+[*&]*([A-Za-z_$][\w$]*)[^\S\n]*\(")
_NOT_A_DECL = frozenset({
    "if", "for", "while", "switch", "catch", "return", "else", "do", "with",
})


def _is_statement(line):
    """True when TYPED_DECL matched a call site, not a declaration.

    `throw makeError(x);`, `case handle(x):` and every other statement this
    guards against ends in `;` or `:` once a trailing comment is gone -- a
    declaration's line instead ends in `{` (`int f() {`) or in the closing
    `)` of an Allman-style header whose brace sits on the next line. A `/*`
    with no matching `*/` on this line opens a comment that runs past it,
    hiding text we cannot see; guessing at what that text is risks the same
    false positive this check exists to reject, so the unresolved case is
    treated as a statement -- the safe side of the guess, since the cost of
    wrongly discarding a declaration's comment is lower than the cost of
    misattributing one to a call site's name.
    """
    open_at = line.rfind("/*")
    if open_at != -1:
        close_at = line.find("*/", open_at + 2)
        if close_at == -1:
            return True
        line = line[:open_at] + line[close_at + 2:]
    code = line.split("//", 1)[0].rstrip()
    return code.endswith((";", ":"))


def declarations_brace(text):
    out = {}
    for number, line in enumerate(text.splitlines(), start=1):
        match = _KEYWORD_DECL.match(line) or _ARROW_DECL.match(line)
        typed = match is None
        if typed:
            match = _TYPED_DECL.match(line)
        if not match or (typed and _is_statement(line)):
            continue
        name = match.group(1)
        head = line.strip().split("(")[0].split()
        if name in _NOT_A_DECL or (head and head[0] in _NOT_A_DECL):
            continue
        out[number] = name
    return out


def declarations(text, ext):
    """Declaration line -> name, for whichever reader covers `ext`."""
    if ext == ".py":
        return declarations_python(text)
    if ext in BRACE_EXT:
        return declarations_brace(text)
    return {}
