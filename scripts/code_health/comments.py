"""Comments that carry no information beyond the code next to them.

Three detectors, two of which count toward the metric. `commented_out` ports
eradicate's algorithm and ends in a real `ast.parse`; `trivial_doc` ports
Steidl's coherence coefficient (ICPC 2013), whose 0.5 threshold was validated
by a 16-developer survey. `short_inline` is measured at 0.70 precision and is
therefore reported in evidence only -- summing three classes would give the
whole metric the precision of its weakest member.

Two immunity passes, not one, because they protect different things.
`is_directive` -- tool directives, task tags, licence text, issue references
and URLs, shebang and coding declarations -- gates EVERY detector,
`commented_out` included. `is_prose_immune` -- the 30-word rule, reason words,
units, range bounds, null-value wording and ownership verbs -- gates only the
prose detectors, because commented-out code is code: `# return None` is a
return statement, not documentation of null semantics, and only the directive
half may excuse it. Steidl and Pascarella independently measured that licence
headers, task tags and tool directives make up most of a repository's comment
volume; without the directive pass this module measures copyright.
"""

import ast
import io
import os
import re
import tokenize
from collections import namedtuple

# 1-based lines. `text` has comment markers stripped; consecutive own-line
# comments are merged into one record, because every threshold below is
# defined over a comment as a unit, not over a physical line. `docstring` is
# true only for records `_python_docstrings` produces -- eradicate's own
# algorithm never examines anything but a `#` line comment, and extending it
# to a docstring was the mistake behind every `commented_out` false positive
# found on live code (Task 9): a JSON schema worked example in `xhh/ajax.py`
# and `feature_id:` / `code_owner:` metadata in `resume-profile-front` both
# read as majority-code line by line while being documentation, not a
# commented-out block. `classify_file` uses this field to keep the gate
# out of a docstring's way while still running `trivial_doc` on it. `owner`
# is the name of the declaration a docstring documents, carried from the AST
# node that produced it; it is `None` for every `#` comment (whose owner is
# resolved by line arithmetic in `classify_file`) and for a module docstring,
# which documents a file, not a name.
Comment = namedtuple("Comment", "line end_line text inline docstring owner",
                     defaults=(None,))


def extract_python(text):
    """Comments and docstrings of one Python source, or None if it does not parse.

    Two independent failures, both `None`, because `tokenize` is lexical and
    succeeds on plenty of sources `ast.parse` rejects. The common trigger is
    not a typo: it is syntax newer than the interpreter running the audit --
    PEP 695 `type X = int` on 3.11, `match` on 3.9. Returning the `#` comments
    alone there would report a file whose docstrings were never extracted and
    whose declaration table was never built as `trivial_doc: count 0`, i.e. a
    measured zero for a file nothing in this module could read.
    """
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return None
    docstrings = _python_docstrings(text)
    if docstrings is None:
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
                           inline=bool(before.strip()), docstring=False))
    # Line order, not "every `#` comment then every docstring". `audit` asks a
    # human to read `evidence.zero_coherence` (skills/audit/SKILL.md), and that
    # list is built in this list's order -- interleaved by source position is
    # the only order in which a reader can follow it down the file.
    return sorted(_merge_runs(raw) + docstrings, key=lambda c: c.line)


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
    """Docstring records with their owner, or None if `text` does not parse.

    `None` and `[]` are different answers: `[]` is "parsed, has no docstring",
    `None` is "never parsed". Only the caller can tell them apart, and
    `extract_python` turns the second into a `None` for the whole file.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, _DOC_NODES):
            continue
        if ast.get_docstring(node) is None:
            continue
        expr = node.body[0]
        out.append(Comment(line=expr.lineno, end_line=expr.end_lineno,
                           text=ast.get_docstring(node).strip(), inline=False,
                           docstring=True,
                           owner=getattr(node, "name", None)))
    return sorted(out, key=lambda c: c.line)


# Extensions with a verified `//` + `/* */` reader AND a `declarations_brace`
# resolution verified by the per-extension probe in `references/tools.md`. Two
# admission tests, not one: a reader that finds the comments but cannot name
# the declaration under them reports `trivial_doc: 0` on every file, which
# reads as "checked, found nothing" for a language whose declarations nobody
# can see -- the same silent zero this repository already refuses for lizard's
# nesting column.
#
# Deliberately absent, each named with its evidence in `references/tools.md`:
# `.lua` (`--`), `.rb` (`#` plus `=begin`) and `.php` (three comment syntaxes,
# one of them `#`) have no comment reader here at all; `.m` and `.mm` have one
# but fail the declaration probe -- Objective-C's `- (void)loadAll:(int)x {`
# matches none of the three declaration shapes below, and `.m` is MATLAB at
# least as often as it is Objective-C, where `%` comments are invisible to
# this reader and the transpose `'` opens a phantom string literal that
# swallows the rest of the file.
BRACE_EXT = frozenset({
    ".c", ".h", ".cpp", ".cc", ".hpp", ".cs", ".java",
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
    ".go", ".kt", ".rs", ".swift", ".scala",
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

# Steidl's first length rule: at this length or below, 70% of developers chose
# to delete the comment or to fold its text into a function name. 0.70 is below
# the 0.85 floor a counted class must clear -- this plugin's own rule, set in
# `references/quality-policy.md` and inherited from nowhere -- so `short_inline`
# is reported and never summed: three classes added together would carry the
# precision of the weakest one.
SHORT_INLINE_WORDS = 2

COUNTED_CLASSES = ("commented_out", "trivial_doc")

# eradicate v3.0.1's pipeline. The final step is a real `ast.parse`, which is
# why this class is the most accurate of the three; everything above it exists
# to stop prose from ever reaching the parser. The 0.89/0.95 figures Steidl
# reports are that paper's, for its own algorithm -- see
# `references/metrics.md`, "Where `comment_noise`'s precision figures come
# from", for what this implementation actually measured.
_CODE_INDICATORS = ("(", ")", "[", "]", "{", "}", ":", "=", "%")
_CODE_WORDS = ("print", "return", "break", "continue", "import", "pass", "raise")
_MULTILINE_ASSIGN = re.compile(r"^\s*\w+\s*=.*[(\[{]$")
_BRACKETS_ONLY = re.compile(r"^[\s)\]}]+[,:]?$")
_DICT_ITEM = re.compile(r"""^\s*['"]\w+['"]\s*:.+[,{]\s*$""")
_BLOCK_KEYWORD = re.compile(r"^\s*(if|elif|else|for|while|with|try|except|finally|def|class|async)\b.*:\s*$")
# `\b` alone is not a token boundary here: it fires on the transition between
# a word character and a hyphen, so the old pattern matched "continue" inside
# "continue-on-error" and flagged `except Exception as exc:  # continue-on-error
# (CancelledError propagates` as commented-out code (real comment,
# jira-resource-allocation). The lookahead below additionally refuses a
# following hyphen, which `\b` alone does not.
_STATEMENT_KEYWORD = re.compile(r"^\s*(return|break|continue|yield|raise)(?![\w-]).*")

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


def is_commented_out(text):
    """True when the comment body parses or reads as Python source."""
    if is_directive(text):
        return False
    lines = text.splitlines()
    non_empty_lines = [line for line in lines if line.strip()]
    if not non_empty_lines:
        return False
    # A comment is code when more than half its non-empty lines read as code.
    # A fifteen-line prose docstring with one worked example containing `else:`
    # should not be flagged as commented-out code by a single structural match.
    code_looking = sum(1 for line in non_empty_lines if _line_is_code(line))
    return code_looking > len(non_empty_lines) / 2


def _is_bare_expr_without_call(tree):
    """True if the parsed line is a bare expression with no function calls.

    Prose like "C/Java/C# typed form" parses as valid Python (a binary
    expression), but is documentation, not commented-out code. A bare
    expression with no calls is likely prose that happened to parse.
    """
    # Single expression statement with no calls is likely prose
    if len(tree.body) == 1 and isinstance(tree.body[0], ast.Expr):
        expr = tree.body[0].value
        # Check if there are any Call nodes anywhere in the expression
        for node in ast.walk(expr):
            if isinstance(node, ast.Call):
                return False  # Has calls, so it's likely code
        return True  # No calls found, so it's likely prose

    return False  # Not a single bare expression


# A bare Name is a real annotation only for a builtin or typing type -- this
# is what tells `x: int` (code) apart from `Казахстан: Астана` (a real
# false positive, xhh/utils/employerview_meta_tags.py: a Country:Capital
# label comment, valid syntax only because Cyrillic identifiers are legal
# Python 3 names). The list is Python's own vocabulary for this position,
# not a fact about any repository this module has been run against.
_ANNOTATION_TYPE_NAMES = frozenset({
    "str", "int", "float", "bool", "bytes", "bytearray", "complex",
    "list", "dict", "set", "frozenset", "tuple", "object", "type", "None",
    "Any", "Optional", "Union", "Literal", "Callable", "Iterable",
    "Sequence", "Mapping",
})


def _is_type_expression(node):
    """True when `node` has a shape only a real type annotation uses.

    `dict[str, Any]` is a Subscript, `typing.Optional[Config]` an Attribute
    (dotted name) feeding a Subscript, `int | None` a BinOp/BitOr of two type
    expressions -- all three are commented-out code when found as a valueless
    annotation. A bare Name is trusted only against `_ANNOTATION_TYPE_NAMES`:
    the allow-list cannot tell a project-local type name (`cfg: Config`,
    perfectly legitimate annotation grammar) from an ordinary word sitting in
    the same position, so it accepts only names it already knows, and a
    local alias is a false negative taken deliberately -- a precision-for-
    recall trade, not a grammar violation. Anything else in this position --
    a Compare, a Call, an arbitrary Name not on the list -- is prose that
    happened to parse, however code-shaped its colon makes it look.
    """
    if isinstance(node, (ast.Subscript, ast.Attribute)):
        return True
    if isinstance(node, ast.Name):
        return node.id in _ANNOTATION_TYPE_NAMES
    if isinstance(node, ast.Constant):
        # `x: None` is the one bare-Constant annotation that is real code;
        # every other Constant (str, int, ...) is Fix 3's original case --
        # `Format: "MemAvailable:   1234567 kB"` (real comment, a /proc/meminfo
        # parser) -- kept rejected because a string annotation is a forward
        # reference indistinguishable from prose here, and prose is the
        # commoner case in a comment.
        return node.value is None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _is_type_expression(node.left) and _is_type_expression(node.right)
    return False


def _is_prose_annotation(tree):
    """True if the line is a valueless annotation whose annotation is not a type expression.

    `clusters: dict[str, Any]` (real deprecated annotation, xhh) is a
    valueless AnnAssign whose annotation is a Subscript -- a type expression
    -- and IS commented-out code. `Казахстан:Астана` (real false positive,
    xhh) is the same AnnAssign shape with a bare Name that is not a known
    type. The discriminator is the annotation node's shape against Python's
    own type-annotation grammar, not whether a value is present.
    """
    if len(tree.body) != 1:
        return False
    stmt = tree.body[0]
    if not (isinstance(stmt, ast.AnnAssign) and stmt.value is None):
        return False
    return not _is_type_expression(stmt.annotation)


def _has_call_with_space_before_parenthesis(tree, line):
    """True when a call in the parsed line has whitespace before its "(".

    `lower_is_better (default)` parses as Expr(Call) and reads as code, but
    is prose (real comment, a metrics-direction note). PEP 8 forbids
    whitespace between a callee and its argument list, so real Python
    essentially never has it while English prose often does; contrast the
    genuine commented-out call `put_oauth_error_state(self,
    HTTPStatus.FORBIDDEN)` (real comment, xhh), which has none.

    `end_col_offset` is a UTF-8 **byte** offset, so the line is indexed as
    bytes. Indexing it as characters shifts left by one position per
    non-ASCII character already on the line: `кэш(x, y)` reports
    `end_col_offset=6` against a 9-character, 12-byte line, and `line[6]` is
    the space inside the argument list -- so genuinely commented-out code
    with a Cyrillic identifier was silently rejected as prose. Two of the
    three repositories this discriminator was tuned against are
    Russian-language.
    """
    raw = line.encode("utf-8")
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            end = node.func.end_col_offset
            if end is not None and end < len(raw) and raw[end:end + 1] != b"(":
                return True
    return False


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
        tree = ast.parse(line, mode="exec")
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        # MemoryError: CPython's parser raises this on stack overflow when
        # processing pathological input like a 50k-character unary operator run.
        # Catching it prevents a single malformed comment from crashing the audit.
        return False

    if not tree.body:
        # `BRAND = (214, 0, 28)  # #D6001C -- HH brand red (service.hh),
        # primary accent` (real comment, jira-resource-allocation): the
        # nested '#' before "D6001C" opens a Python comment that truncates
        # everything after it, so this parses -- but to an empty module. An
        # empty parse is not evidence of code.
        return False

    # Bare expressions without calls are prose, not code.
    if _is_bare_expr_without_call(tree):
        return False

    if _is_prose_annotation(tree):
        return False

    if _has_call_with_space_before_parenthesis(tree, line):
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
                               inline=_has_code_before(text, i), docstring=False))
            i = end
            continue
        if ch == "/" and nxt == "*":
            start = i + 2
            end = text.find("*/", start)
            body = text[start:end if end != -1 else n]
            out.append(Comment(line=line, end_line=line + body.count("\n"),
                               text=_strip_stars(body),
                               inline=_has_code_before(text, i), docstring=False))
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
    # Below length 2 every distinct pair sits at edit distance 1 (e.g. "и"
    # vs "a"), so an unfloored rule would match any letter to any other.
    if len(a) < 2 or len(b) < 2:
        return False
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
#
# The typed form carries `::` in two positions, both required by the probe in
# `references/tools.md` and by nothing else: in the return type, so that
# `std::string Cache::name() const {` gets past the prefix; and before the
# captured name, so that an out-of-line C++ member definition
# `int Cache::load_all(int fd) {` reports `load_all` rather than nothing.
# Only the doubled colon is accepted -- a single one would make the JS object
# literal `handler: makeHandler(x),` read as a declaration of `makeHandler`,
# which is the call-site false positive `_is_statement` exists to reject.
_KEYWORD_DECL = re.compile(
    r"^[^\S\n]*(?:[\w@$]+[^\S\n]+)*?"
    r"(?:function|func|fn|class|struct|interface|type|enum|protocol)"
    r"[^\S\n]+(?:\([^)]*\)[^\S\n]+)?([A-Za-z_$][\w$]*)")
_ARROW_DECL = re.compile(
    r"^[^\S\n]*(?:export[^\S\n]+)?(?:const|let|var)[^\S\n]+([A-Za-z_$][\w$]*)"
    r"[^\S\n]*=[^\S\n]*(?:async[^\S\n]+)?(?:\([^)]*\)[^\S\n]*=>|function\b)")
_TYPED_DECL = re.compile(
    r"^[^\S\n]*(?:[\w.<>\[\]*&$]+(?:::[\w.<>\[\]*&$]+)*[^\S\n]+)+[*&]*"
    r"(?:[A-Za-z_$][\w$]*::)*([A-Za-z_$][\w$]*)[^\S\n]*\(")
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


def classify_file(text, ext, chars_per_token=4):
    """One file's comment classes, or None when no reader covers `ext`."""
    found = extract(text, ext)
    if found is None:
        return None
    decls = declarations(text, ext)
    buckets = {"commented_out": [], "trivial_doc": [], "short_inline": []}
    zero, noise_chars, immune = [], 0, 0

    for item in found:
        # Directives (eradicate's whitelist) gate every detector, including
        # `commented_out`: a `# type: ignore` line is tooling metadata even
        # when its body also happens to parse as code.
        if is_directive(item.text):
            immune += 1
            continue
        # Commented-out code is code, not prose -- checked ahead of the prose
        # gate so a line like `# return None` is classified as a return
        # statement, never protected as documentation of null semantics.
        # A docstring never reaches this gate: eradicate itself only ever
        # examines a `#` line comment, and every live false positive found
        # against real repositories (Task 9) was a docstring whose worked
        # example -- a JSON schema in `xhh/ajax.py`, `feature_id:` metadata
        # in `resume-profile-front` -- out-numbered its own prose line by
        # line. A docstring instead falls through to `trivial_doc`, which is
        # its main home.
        if ext == ".py" and not item.docstring and is_commented_out(item.text):
            klass = "commented_out"
        else:
            if is_prose_immune(item.text):
                immune += 1
                continue
            # A `#` comment's owner is ambiguous by position -- above a `def`
            # or trailing after a statement -- so it is resolved by line
            # arithmetic, following declaration first. A docstring's owner is
            # not ambiguous at all: it is the declaration it is nested in, and
            # `_python_docstrings` already had that node in hand. Deriving it
            # by line arithmetic instead attributed a factory function's
            # docstring to the function it builds -- `def build_handler():` /
            # `"""Make the handler."""` / `def make_the_handler():` scored
            # 1.00 against the nested function and was counted, against 0.33
            # for its real owner -- and attributed a module docstring to
            # whatever happened to be declared on line 2.
            name = item.owner if item.docstring else (
                decls.get(item.end_line + 1) or decls.get(item.line - 1))
            score = coherence(item.text, name) if name else None
            if score is not None and score > TRIVIAL_COHERENCE:
                klass = "trivial_doc"
            elif item.inline and len(words_of(item.text)) <= SHORT_INLINE_WORDS:
                klass = "short_inline"
            else:
                # Steidl: a score of exactly 0 means the comment shares no word
                # with the name it documents -- either it carries information the
                # name does not, or the name is wrong. Never noise on its own;
                # `audit` folds it into an existing complexity group as evidence.
                if score == 0:
                    zero.append({"line": item.line, "function": name})
                continue
        buckets[klass].extend(range(item.line, item.end_line + 1))
        if klass in COUNTED_CLASSES:
            noise_chars += len(item.text)

    evidence = {"tokens": -(-noise_chars // chars_per_token), "immune": immune,
                "zero_coherence": zero}
    for klass, lines in buckets.items():
        if klass == "commented_out" and ext != ".py":
            evidence[klass] = None
            continue
        evidence[klass] = {"lines": sorted(lines), "count": len(lines)}

    return {
        "noise_comment_lines": sum(
            len(buckets[k]) for k in COUNTED_CLASSES
            if evidence.get(k) is not None),
        "evidence": evidence,
    }


def measure_comments(repo_root, inventory, cfg):
    """The layers.comments object. Always returns a status; never raises."""
    rows, langs = [], set()
    # Three unrelated causes, counted apart. One "no verified comment reader"
    # for all three told a reader that a `.rb` file, a file the process cannot
    # open, and a file using syntax newer than the interpreter were the same
    # problem -- only the first is about this module's language coverage, and
    # only the first is unfixable by the person reading the report.
    no_reader, unreadable, unparsed = 0, 0, 0
    chars_per_token = cfg["chars_per_token"]
    for rel in inventory.source:
        ext = os.path.splitext(rel)[1].lower()
        if ext != ".py" and ext not in BRACE_EXT:
            no_reader += 1
            continue
        try:
            text = (repo_root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            unreadable += 1
            continue
        got = classify_file(text, ext, chars_per_token)
        if got is None:
            unparsed += 1
            continue
        langs.add("python" if ext == ".py" else "brace")
        rows.append(dict(path=rel, **got))

    reasons = []
    if no_reader:
        reasons.append("%d file(s) have no comment reader for their extension"
                       % no_reader)
    if unreadable:
        reasons.append("%d file(s) could not be read from disk" % unreadable)
    if unparsed:
        reasons.append("%d Python file(s) do not parse" % unparsed)
    # `commented_out` is Python-only, so a repository with no measured Python
    # file has one of the two counted classes unrun. Its per-file evidence is
    # already `null` rather than an empty list; the layer must say so too.
    # DESIGN.md §5: "The rule is per metric, not only per layer... a metric
    # computed from a layer that is not `measured` and coming out empty is
    # reported as `null` and its own layer drops to `partial` with the
    # dependency named." `derived.symbol_collisions` already obeys it.
    if rows and "python" not in langs:
        reasons.append("commented_out is Python-only and this repository has "
                       "no measured Python file")

    layer = {"files": rows, "languages_measured": sorted(langs),
             "not_measured_files": no_reader + unreadable + unparsed,
             "reason": "; ".join(reasons) or None}
    if not rows:
        layer["status"] = "not_measured"
    elif reasons:
        layer["status"] = "partial"
    else:
        layer["status"] = "measured"
    return layer
