# Comment Noise Metric Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Новый слой `comments` в `measure.py` находит комментарии, не несущие информации сверх кода рядом, и отдаёт их аудиту как долг с находкой `comment_noise`.

**Architecture:** Один модуль `scripts/code_health/comments.py` без внешних зависимостей. Извлечение комментариев (Python через `tokenize` + `ast`, brace-языки через сканер с учётом строковых литералов), затем проход иммунитета, затем два детектора, чьё срабатывание входит в значение метрики, и один, чьё срабатывание идёт только в `evidence`. Метрика `noise_comment_lines` растёт при ухудшении, поэтому `config.level_for` работает без правки.

**Tech Stack:** Python 3, только stdlib (`tokenize`, `ast`, `re`, `unittest`). Никаких новых внешних инструментов, слой `comments` не попадает в `LAYER_TOOLS`.

**Spec:** `docs/superpowers/specs/2026-09-09-comment-noise-design.md`

## Global Constraints

- Только stdlib. Слой не запускает подпроцессов и не ходит в сеть.
- Не измерили — значит `None`, никогда не `0` и не пустой список (`DESIGN.md:161-164`).
- Порог сравнивается ровно одной функцией `config.level_for`; переопределять правило `value >= threshold` где-либо ещё запрещено (`scripts/code_health/config.py:1-6`).
- Тесты — stdlib `unittest`, запуск `python3 -m unittest discover -s tests -t tests`. Ни pytest, ни conftest.
- Точность класса, входящего в значение метрики, — не ниже 0.85 на корпусе. Класс ниже уходит в `evidence`.
- Приоритет группы `comment_noise` в аудите — потолок P2.
- Один коммит на задачу. Не пушить.

---

### Task 1: Извлечение комментариев из Python

**Files:**
- Create: `scripts/code_health/comments.py`
- Test: `tests/test_comments.py`

**Interfaces:**
- Consumes: ничего
- Produces: `Comment = namedtuple("Comment", "line end_line text inline")` (1-based строки, `text` без маркеров комментария, `inline=True` если на той же строке слева есть код); `extract_python(text) -> list[Comment] | None` (`None` при синтаксической ошибке)

- [ ] **Step 1: Написать падающий тест**

```python
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from code_health import comments as CM  # noqa: E402


class PythonExtractionTests(unittest.TestCase):
    def test_own_line_comment(self):
        got = CM.extract_python("# set up\nx = 1\n")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].text, "set up")
        self.assertFalse(got[0].inline)
        self.assertEqual(got[0].line, 1)

    def test_trailing_comment_is_inline(self):
        got = CM.extract_python("x = 1  # counter\n")
        self.assertTrue(got[0].inline)

    def test_hash_inside_string_is_not_a_comment(self):
        got = CM.extract_python('url = "http://x/#frag"\n')
        self.assertEqual(got, [])

    def test_consecutive_own_line_comments_merge_into_one_block(self):
        got = CM.extract_python("# first\n# second\nx = 1\n")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].text, "first\nsecond")
        self.assertEqual(got[0].line, 1)
        self.assertEqual(got[0].end_line, 2)

    def test_inline_comment_never_merges_with_the_next_line(self):
        got = CM.extract_python("x = 1  # a\n# b\n")
        self.assertEqual(len(got), 2)

    def test_docstring_is_extracted_as_a_comment(self):
        got = CM.extract_python('def f():\n    """Return f."""\n    return 1\n')
        texts = [c.text for c in got]
        self.assertIn("Return f.", texts)

    def test_syntax_error_returns_none_not_empty(self):
        self.assertIsNone(CM.extract_python("def (:\n"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Прогнать и убедиться, что падает**

Run: `python3 -m unittest discover -s tests -t tests -k PythonExtraction -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'code_health.comments'`

- [ ] **Step 3: Реализовать**

```python
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
```

- [ ] **Step 4: Прогнать и убедиться, что проходит**

Run: `python3 -m unittest discover -s tests -t tests -k PythonExtraction -v`
Expected: PASS, 7 тестов

- [ ] **Step 5: Коммит**

```bash
git add scripts/code_health/comments.py tests/test_comments.py
git commit -m "feat(code-health): extract Python comments and docstrings as units"
```

---

### Task 2: Извлечение комментариев из brace-языков

**Files:**
- Modify: `scripts/code_health/comments.py`
- Test: `tests/test_comments.py`

**Interfaces:**
- Consumes: `Comment` из Task 1
- Produces: `extract_brace(text) -> list[Comment]`; `BRACE_EXT` (frozenset расширений); `extract(text, ext) -> list[Comment] | None` — диспетчер, `None` для языка без ридера

- [ ] **Step 1: Написать падающий тест**

```python
class BraceExtractionTests(unittest.TestCase):
    def test_line_comment(self):
        got = CM.extract_brace("int x = 1; // counter\n")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].text, "counter")
        self.assertTrue(got[0].inline)

    def test_block_comment_spans_lines(self):
        got = CM.extract_brace("/* one\n   two */\nx();\n")
        self.assertEqual(got[0].line, 1)
        self.assertEqual(got[0].end_line, 2)
        self.assertFalse(got[0].inline)

    def test_slashes_inside_a_string_are_not_a_comment(self):
        self.assertEqual(CM.extract_brace('var u = "http://x";\n'), [])

    def test_escaped_quote_does_not_end_the_string(self):
        self.assertEqual(CM.extract_brace('var s = "a\\" // b";\n'), [])

    def test_template_literal_is_a_string(self):
        self.assertEqual(CM.extract_brace("var s = `a // b`;\n"), [])

    def test_consecutive_line_comments_merge(self):
        got = CM.extract_brace("// a\n// b\nx();\n")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].text, "a\nb")

    def test_unterminated_line_comment_at_eof_is_kept(self):
        got = CM.extract_brace("x(); // trailing")
        self.assertEqual(len(got), 1)


class DispatchTests(unittest.TestCase):
    def test_unknown_extension_returns_none_not_empty(self):
        self.assertIsNone(CM.extract("-- lua comment\n", ".lua"))

    def test_python_extension_routes_to_the_python_reader(self):
        self.assertEqual(len(CM.extract("# x\n", ".py")), 1)

    def test_brace_extension_routes_to_the_brace_reader(self):
        self.assertEqual(len(CM.extract("// x\n", ".ts")), 1)
```

- [ ] **Step 2: Прогнать и убедиться, что падает**

Run: `python3 -m unittest discover -s tests -t tests -k "BraceExtraction or Dispatch" -v`
Expected: FAIL, `AttributeError: module 'code_health.comments' has no attribute 'extract_brace'`

- [ ] **Step 3: Реализовать**

```python
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
```

- [ ] **Step 4: Прогнать и убедиться, что проходит**

Run: `python3 -m unittest discover -s tests -t tests -k "BraceExtraction or Dispatch" -v`
Expected: PASS, 10 тестов

- [ ] **Step 5: Коммит**

```bash
git add scripts/code_health/comments.py tests/test_comments.py
git commit -m "feat(code-health): brace-language comment reader with literal skipping"
```

---

### Task 3: Проход иммунитета

**Files:**
- Modify: `scripts/code_health/comments.py`
- Test: `tests/test_comments.py`

**Interfaces:**
- Consumes: `Comment` из Task 1
- Produces: `is_immune(text) -> bool`

- [ ] **Step 1: Написать падающий тест**

```python
class ImmunityTests(unittest.TestCase):
    IMMUNE = [
        "noqa: E501",
        "type: ignore[arg-type]",
        "eslint-disable-next-line no-console",
        "TODO: drop after RAM-436",
        "FIXME later",
        "Copyright 2026 Acme",
        "SPDX-License-Identifier: MIT",
        "see #1234 for the original report",
        "see https://bugs.example/1 for why",
        "ms, not seconds",
        "end is exclusive",
        "returns None when the cache is cold",
        "the caller owns the handle and must close it",
        "workaround for a driver bug",
        "иначе счётчик сбрасывается на втором проходе",
        "-*- coding: utf-8 -*-",
        " ".join(["word"] * 30),
    ]
    NOT_IMMUNE = [
        "increment counter",
        "loop through the items",
        "removes all defined markers",
        "set x to 1",
    ]

    def test_immune_texts(self):
        for text in self.IMMUNE:
            with self.subTest(text=text):
                self.assertTrue(CM.is_immune(text))

    def test_not_immune_texts(self):
        for text in self.NOT_IMMUNE:
            with self.subTest(text=text):
                self.assertFalse(CM.is_immune(text))
```

- [ ] **Step 2: Прогнать и убедиться, что падает**

Run: `python3 -m unittest discover -s tests -t tests -k Immunity -v`
Expected: FAIL, `AttributeError: ... has no attribute 'is_immune'`

- [ ] **Step 3: Реализовать**

```python
import re

# A comment matching any of these is never classified as noise. The list is
# the whole false-positive budget: Steidl (ICPC 2013) measured licence headers
# at 18-49% of comment volume and Pascarella (MSR 2017) put metadata, task and
# directive lines at 59% of comment LINES, so without this pass the metric
# reports copyright.
_IMMUNE_PATTERNS = (
    r"\bnoqa\b|\btype:\s*ignore|\beslint-disable|\bpylint\b|\bpyright\b"
    r"|\bnosec\b|\bfmt:\s*(on|off)|\bisort:|\bNOSONAR\b|\bgo:generate\b"
    r"|\bruff:\s|\bmypy:",
    r"\bTODO\b|\bFIXME\b|\bXXX\b|\bHACK\b",
    r"\bcopyright\b|\blicen[sc]e\b|\bSPDX\b",
    r"#\d+|\b[A-Z]{2,}-\d+\b|https?://",
    r"\bbecause\b|\bworkaround\b|\botherwise\b"
    r"|\bиначе\b|\bпотому что\b|\bпоэтому\b",
    r"\bms\b|\bmsec\b|\bsec\b|\bbytes?\b|\bUTC\b|\bepoch\b"
    r"|\b[01]-based\b|\bpx\b|\bhz\b",
    r"\binclusive\b|\bexclusive\b|\bвключительно\b",
    r"\bnull\b|\bNone\b|\bnil\b|\bundefined\b|\bempty\b",
    r"\bcaller must\b|\bowns\b|\bfrees\b|\bcloses\b|\breleases\b|освобожда",
    r"^#!|-\*-\s*coding",
)
_IMMUNE_RE = re.compile("|".join("(?:%s)" % p for p in _IMMUNE_PATTERNS),
                        re.IGNORECASE | re.MULTILINE)

# Steidl's second validated length rule: developers kept the comment in 10 of
# 10 examples at this length, with >=88% agreement in nine of them.
LONG_COMMENT_WORDS = 30

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def words_of(text):
    """Alphabetic words of a comment, lowercased, after marker normalisation."""
    return [w.lower() for w in _WORD_RE.findall(text)]


def is_immune(text):
    if len(words_of(text)) >= LONG_COMMENT_WORDS:
        return True
    return bool(_IMMUNE_RE.search(text))
```

- [ ] **Step 4: Прогнать и убедиться, что проходит**

Run: `python3 -m unittest discover -s tests -t tests -k Immunity -v`
Expected: PASS, 2 теста, 21 subTest

- [ ] **Step 5: Коммит**

```bash
git add scripts/code_health/comments.py tests/test_comments.py
git commit -m "feat(code-health): immunity pass so the metric does not measure copyright"
```

---

### Task 4: Детектор `commented_out`

**Files:**
- Modify: `scripts/code_health/comments.py`
- Test: `tests/test_comments.py`

**Interfaces:**
- Consumes: `is_immune`, `Comment`
- Produces: `is_commented_out(text) -> bool` — только Python-синтаксис

- [ ] **Step 1: Написать падающий тест**

```python
class CommentedOutTests(unittest.TestCase):
    CODE = [
        "print(old_result)",
        "return None",
        "x = compute(a, b)",
        "import json",
        "if ready:",
        "for row in rows:",
        '"key": value,',
        "result = fetch(",
    ]
    # Every entry is an open ruff ERA001 false-positive report.
    NOT_CODE = [
        "TODO: remove print(x) once the API lands",
        "see issue #10072 for the original example",
        "the helper must return early here",
        "/// script\n/// requires-python = \">=3.11\"",
        "ruff: ignore[E501]",
        "a plain sentence about the parser",
        "-*- coding: utf-8 -*-",
    ]

    def test_code_is_detected(self):
        for text in self.CODE:
            with self.subTest(text=text):
                self.assertTrue(CM.is_commented_out(text))

    def test_prose_is_not_detected(self):
        for text in self.NOT_CODE:
            with self.subTest(text=text):
                self.assertFalse(CM.is_commented_out(text))
```

- [ ] **Step 2: Прогнать и убедиться, что падает**

Run: `python3 -m unittest discover -s tests -t tests -k CommentedOut -v`
Expected: FAIL, `AttributeError: ... has no attribute 'is_commented_out'`

- [ ] **Step 3: Реализовать**

```python
# eradicate v3.0.1's pipeline. The final step is a real parse, which is why
# this class is the most accurate of the three (P 0.89 / R 0.95, Steidl 2013);
# everything above it exists to stop prose from ever reaching `compile`.
_CODE_INDICATORS = ("(", ")", "[", "]", "{", "}", ":", "=", "%")
_CODE_WORDS = ("print", "return", "break", "continue", "import", "pass", "raise")
_MULTILINE_ASSIGN = re.compile(r"^\s*\w+\s*=.*[(\[{]$")
_BRACKETS_ONLY = re.compile(r"^[\s)\]}]+[,:]?$")
_DICT_ITEM = re.compile(r"""^\s*['"]\w+['"]\s*:.+[,{]\s*$""")
_BLOCK_KEYWORD = re.compile(r"^\s*(else|try|finally|except\b.*|elif\b.*)\s*:\s*$")


def is_commented_out(text):
    """True when the comment body parses or reads as Python source."""
    if is_immune(text):
        return False
    return any(_line_is_code(line) for line in text.splitlines())


def _line_is_code(line):
    line = line.strip()
    if not line:
        return False
    if not (any(ind in line for ind in _CODE_INDICATORS)
            or any(re.search(r"\b%s\b" % w, line) for w in _CODE_WORDS)):
        return False
    if _BLOCK_KEYWORD.match(line) or _BRACKETS_ONLY.match(line):
        return True
    if _MULTILINE_ASSIGN.match(line) or line.endswith("\\") or _DICT_ITEM.match(line):
        return True
    try:
        compile(line, "<comment>", "exec")
    except (SyntaxError, ValueError):
        return False
    return True
```

- [ ] **Step 4: Прогнать и убедиться, что проходит**

Run: `python3 -m unittest discover -s tests -t tests -k CommentedOut -v`
Expected: PASS, 2 теста, 15 subTest

- [ ] **Step 5: Коммит**

```bash
git add scripts/code_health/comments.py tests/test_comments.py
git commit -m "feat(code-health): commented-out code detector, eradicate's pipeline"
```

---

### Task 5: Детектор `trivial_doc` (coherence coefficient)

**Files:**
- Modify: `scripts/code_health/comments.py`
- Test: `tests/test_comments.py`

**Interfaces:**
- Consumes: `words_of`, `is_immune`
- Produces: `split_identifier(name) -> list[str]`; `coherence(text, identifier) -> float | None`; `TRIVIAL_COHERENCE = 0.5`; `declarations_python(text) -> dict[int, str]` и `declarations_brace(text) -> dict[int, str]` (номер строки декларации → её имя); `declarations(text, ext) -> dict[int, str]`

- [ ] **Step 1: Написать падающий тест**

```python
class CoherenceTests(unittest.TestCase):
    def test_identifier_splits_on_camel_and_snake(self):
        self.assertEqual(CM.split_identifier("removeAllMarkers"),
                         ["remove", "all", "markers"])
        self.assertEqual(CM.split_identifier("remove_all_markers"),
                         ["remove", "all", "markers"])
        self.assertEqual(CM.split_identifier("HTTPServerError"),
                         ["http", "server", "error"])

    def test_paper_example_scores_three_quarters(self):
        # Steidl, ICPC 2013: this exact pair is the paper's worked example.
        got = CM.coherence("removes all defined markers", "removeAllMarkers")
        self.assertAlmostEqual(got, 0.75)

    def test_plural_matches_within_levenshtein_one(self):
        self.assertAlmostEqual(CM.coherence("markers", "marker"), 1.0)

    def test_two_edits_apart_do_not_match(self):
        self.assertAlmostEqual(CM.coherence("marker", "mask"), 0.0)

    def test_comment_with_no_words_returns_none(self):
        self.assertIsNone(CM.coherence("-----", "anything"))

    def test_informative_comment_scores_below_the_threshold(self):
        got = CM.coherence("markers survive a reload; the cache does not",
                           "removeAllMarkers")
        self.assertLess(got, CM.TRIVIAL_COHERENCE)


class DeclarationTests(unittest.TestCase):
    def test_function_and_class_names_by_line(self):
        src = "class Cache:\n    def get_key(self):\n        return 1\n"
        self.assertEqual(CM.declarations_python(src), {1: "Cache", 2: "get_key"})

    def test_decorated_function_reports_the_def_line(self):
        src = "@cached\ndef load_all():\n    return []\n"
        self.assertEqual(CM.declarations_python(src), {2: "load_all"})

    def test_unparseable_source_returns_empty(self):
        self.assertEqual(CM.declarations_python("def (:\n"), {})


class BraceDeclarationTests(unittest.TestCase):
    def test_keyword_forms(self):
        self.assertEqual(CM.declarations_brace("function returnTotalPrice() {}\n"),
                         {1: "returnTotalPrice"})
        self.assertEqual(CM.declarations_brace("export class UserCache {}\n"),
                         {1: "UserCache"})
        self.assertEqual(CM.declarations_brace("func LoadAll() error {\n"),
                         {1: "LoadAll"})
        self.assertEqual(CM.declarations_brace("fn parse_all(x: u8) {\n"),
                         {1: "parse_all"})

    def test_c_style_typed_declaration(self):
        self.assertEqual(CM.declarations_brace("static int read_block(int fd) {\n"),
                         {1: "read_block"})

    def test_call_site_is_not_a_declaration(self):
        self.assertEqual(CM.declarations_brace("    doWork(x);\n"), {})

    def test_control_keyword_is_not_a_declaration(self):
        self.assertEqual(CM.declarations_brace("    if (ready) {\n"), {})


class DeclarationDispatchTests(unittest.TestCase):
    def test_python_routes_to_ast(self):
        self.assertEqual(CM.declarations("def f():\n    pass\n", ".py"), {1: "f"})

    def test_brace_routes_to_regex(self):
        self.assertEqual(CM.declarations("function f() {}\n", ".js"), {1: "f"})

    def test_unknown_extension_has_no_declarations(self):
        self.assertEqual(CM.declarations("function f()\n", ".lua"), {})
```

- [ ] **Step 2: Прогнать и убедиться, что падает**

Run: `python3 -m unittest discover -s tests -t tests -k "Coherence or Declaration" -v`
Expected: FAIL, `AttributeError: ... has no attribute 'split_identifier'`

- [ ] **Step 3: Реализовать**

```python
# Steidl (ICPC 2013) section 4: a comment whose words are mostly the words of
# the identifier it sits on is trivial. The 0.5 threshold is the paper's, and
# it was validated by asking 16 developers about 30 examples -- in nine of ten
# trivial cases, over 80% agreed. Correctness of the automatic verdict: ~90%.
TRIVIAL_COHERENCE = 0.5

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


# Two declaration shapes cover the brace languages this module reads: a
# keyword form (`function f`, `class C`, `func F`, `fn f`, `struct S`) and the
# C/Java/C# typed form (`static int read_block(`). A bare `name(` is excluded
# on purpose -- that is a call site, and treating it as a declaration would
# attach every comment above a call to the callee's name.
_KEYWORD_DECL = re.compile(
    r"^[^\S\n]*(?:[\w@$]+[^\S\n]+)*?"
    r"(?:function|func|fn|class|struct|interface|type|enum|protocol)"
    r"[^\S\n]+([A-Za-z_$][\w$]*)")
_TYPED_DECL = re.compile(
    r"^[^\S\n]*(?:[\w.<>\[\]*&$]+[^\S\n]+)+([A-Za-z_$][\w$]*)[^\S\n]*\(")
_NOT_A_DECL = frozenset({
    "if", "for", "while", "switch", "catch", "return", "else", "do", "with",
})


def declarations_brace(text):
    out = {}
    for number, line in enumerate(text.splitlines(), start=1):
        match = _KEYWORD_DECL.match(line) or _TYPED_DECL.match(line)
        if not match:
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
```

- [ ] **Step 4: Прогнать и убедиться, что проходит**

Run: `python3 -m unittest discover -s tests -t tests -k "Coherence or Declaration" -v`
Expected: PASS, 16 тестов

- [ ] **Step 5: Коммит**

```bash
git add scripts/code_health/comments.py tests/test_comments.py
git commit -m "feat(code-health): Steidl coherence coefficient for trivial doc comments"
```

---

### Task 6: Классификация одного файла

**Files:**
- Modify: `scripts/code_health/comments.py`
- Test: `tests/test_comments.py`

**Interfaces:**
- Consumes: всё из Task 1–5
- Produces: `SHORT_INLINE_WORDS = 2`; `COUNTED_CLASSES = ("commented_out", "trivial_doc")`; `classify_file(text, ext, chars_per_token=4) -> dict | None` формы `{"noise_comment_lines": int, "evidence": {"tokens": int, "immune": int, "commented_out": {...} | None, "trivial_doc": {...}, "short_inline": {...}, "zero_coherence": [{"line": int, "function": str}]}}`

- [ ] **Step 1: Написать падающий тест**

```python
class ClassifyFileTests(unittest.TestCase):
    def test_commented_out_and_trivial_doc_count_toward_the_value(self):
        src = ('def remove_all_markers(self):\n'
               '    """Removes all markers."""\n'
               '    # print(self.markers)\n'
               '    self.markers.clear()\n')
        got = CM.classify_file(src, ".py")
        self.assertEqual(got["noise_comment_lines"], 2)
        self.assertEqual(got["evidence"]["trivial_doc"]["lines"], [2])
        self.assertEqual(got["evidence"]["commented_out"]["lines"], [3])

    def test_short_inline_is_reported_but_not_counted(self):
        got = CM.classify_file("n = n + 1  # bump n\n", ".py")
        self.assertEqual(got["noise_comment_lines"], 0)
        self.assertEqual(got["evidence"]["short_inline"]["lines"], [1])

    def test_immune_comment_is_counted_as_immune_only(self):
        got = CM.classify_file("x = 1  # ms, not seconds\n", ".py")
        self.assertEqual(got["noise_comment_lines"], 0)
        self.assertEqual(got["evidence"]["immune"], 1)
        self.assertEqual(got["evidence"]["short_inline"]["lines"], [])

    def test_a_block_of_commented_out_code_counts_every_line(self):
        got = CM.classify_file("# a = 1\n# b = 2\nx = 3\n", ".py")
        self.assertEqual(got["noise_comment_lines"], 2)

    def test_file_with_no_comments_scores_zero(self):
        got = CM.classify_file("x = 1\n", ".py")
        self.assertEqual(got["noise_comment_lines"], 0)

    def test_unreadable_language_returns_none_not_zero(self):
        self.assertIsNone(CM.classify_file("-- x\n", ".lua"))

    def test_unparseable_python_returns_none_not_zero(self):
        self.assertIsNone(CM.classify_file("def (:\n", ".py"))

    def test_brace_file_has_no_commented_out_class(self):
        got = CM.classify_file("// x = 1;\nint y;\n", ".c")
        self.assertEqual(got["evidence"]["commented_out"], None)
        self.assertEqual(got["noise_comment_lines"], 0)

    def test_trivial_doc_fires_in_a_brace_language(self):
        got = CM.classify_file("// returns the total price\n"
                               "function returnTotalPrice() {}\n", ".js")
        self.assertEqual(got["evidence"]["trivial_doc"]["lines"], [1])
        self.assertEqual(got["noise_comment_lines"], 1)

    def test_comment_sharing_no_word_with_its_declaration_is_zero_coherence(self):
        got = CM.classify_file(
            "# callers holding a stale handle crash on the second pass\n"
            "def flush():\n    pass\n", ".py")
        self.assertEqual(got["evidence"]["zero_coherence"],
                         [{"line": 1, "function": "flush"}])
        self.assertEqual(got["noise_comment_lines"], 0)

    def test_zero_coherence_never_counts_as_noise(self):
        got = CM.classify_file("# see #42 for the reason\ndef flush():\n    pass\n",
                               ".py")
        self.assertEqual(got["evidence"]["zero_coherence"], [])
        self.assertEqual(got["evidence"]["immune"], 1)
```

- [ ] **Step 2: Прогнать и убедиться, что падает**

Run: `python3 -m unittest discover -s tests -t tests -k ClassifyFile -v`
Expected: FAIL, `AttributeError: ... has no attribute 'classify_file'`

- [ ] **Step 3: Реализовать**

```python
# Steidl's first length rule: at this length or below, 70% of developers chose
# to delete the comment or to fold its text into a function name. 0.70 is below
# the 0.85 bar this repository sets for a counted class, so `short_inline` is
# reported and never summed -- three classes added together would carry the
# precision of the weakest one.
SHORT_INLINE_WORDS = 2

COUNTED_CLASSES = ("commented_out", "trivial_doc")


def classify_file(text, ext, chars_per_token=4):
    """One file's comment classes, or None when no reader covers `ext`."""
    found = extract(text, ext)
    if found is None:
        return None
    decls = declarations(text, ext)
    buckets = {"commented_out": [], "trivial_doc": [], "short_inline": []}
    zero, noise_chars, immune = [], 0, 0

    for item in found:
        if is_immune(item.text):
            immune += 1
            continue
        name = decls.get(item.end_line + 1) or decls.get(item.line - 1)
        score = coherence(item.text, name) if name else None
        if ext == ".py" and is_commented_out(item.text):
            klass = "commented_out"
        elif score is not None and score > TRIVIAL_COHERENCE:
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
```

- [ ] **Step 4: Прогнать и убедиться, что проходит**

Run: `python3 -m unittest discover -s tests -t tests -k ClassifyFile -v`
Expected: PASS, 12 тестов

- [ ] **Step 5: Коммит**

```bash
git add scripts/code_health/comments.py tests/test_comments.py
git commit -m "feat(code-health): classify one file's comments into the three classes"
```

---

### Task 7: Корпус и измерение точности

**Files:**
- Create: `tests/fixtures/comment_corpus.py`
- Modify: `tests/test_comments.py`

**Interfaces:**
- Consumes: `classify_file`
- Produces: `CORPUS: list[tuple[str, str, str]]` — `(source, ext, expected_class)`, где `expected_class ∈ {"commented_out", "trivial_doc", "short_inline", "clean"}`

Корпус — не покрытие, а измеритель. Без него метрику публиковать нельзя: `references/tools.md:83-86` запрещает публиковать величину, чью корректность никто не проверил.

- [ ] **Step 1: Создать корпус**

```python
"""Labelled comments, one row per behaviour the classifier must get right.

`clean` means the comment must reach no bucket at all: either it is immune or
no detector fires. Rows marked ERA are open ruff ERA001 false-positive reports.
"""

# (source, extension, expected class)
CORPUS = [
    # --- commented_out -------------------------------------------------
    ("# print(old_result)\nx = 1\n", ".py", "commented_out"),
    ("# return None\nx = 1\n", ".py", "commented_out"),
    ("# result = fetch(\n#     url,\n# )\nx = 1\n", ".py", "commented_out"),
    ('# "key": value,\nx = 1\n', ".py", "commented_out"),
    ("# except ValueError:\nx = 1\n", ".py", "commented_out"),
    ("# import json\nx = 1\n", ".py", "commented_out"),

    # --- commented_out: false positives to stay clean (ERA) -------------
    ("# TODO: drop print(x) after RAM-436\nx = 1\n", ".py", "clean"),
    ("# see issue #10072 for the failing example\nx = 1\n", ".py", "clean"),
    ("# /// script\n# requires-python = \">=3.11\"\n# ///\nx = 1\n", ".py", "clean"),
    ("# ruff: ignore[E501]\nx = 1\n", ".py", "clean"),
    ("# -*- coding: utf-8 -*-\nx = 1\n", ".py", "clean"),
    ('url = "http://x/#frag"\n', ".py", "clean"),

    # --- trivial_doc ----------------------------------------------------
    ('def remove_all_markers():\n    """Removes all markers."""\n    pass\n',
     ".py", "trivial_doc"),
    ('def get_user_id():\n    """Get the user id."""\n    return 1\n',
     ".py", "trivial_doc"),
    ("# increment counter\ndef increment_counter():\n    pass\n",
     ".py", "trivial_doc"),
    ("// returns the total price\nfunction returnTotalPrice() {}\n",
     ".js", "trivial_doc"),

    # --- trivial_doc: informative comments to stay clean ----------------
    ('def remove_all_markers():\n'
     '    """Callers holding a marker reference must drop it first."""\n'
     '    pass\n', ".py", "clean"),
    ('def get_user_id():\n    """None while the session is still opening."""\n'
     '    return 1\n', ".py", "clean"),
    ('def parse_window():\n    """Days, not seconds; the caller converts."""\n'
     '    pass\n', ".py", "clean"),

    # --- short_inline ---------------------------------------------------
    ("n = n + 1  # bump n\n", ".py", "short_inline"),
    ("rows.sort()  # sort\n", ".py", "short_inline"),
    ("int y = f();  // call f\n", ".c", "short_inline"),

    # --- short_inline: immune short comments ----------------------------
    ("x = 1  # ms, not seconds\n", ".py", "clean"),
    ("x = 1  # 0-based\n", ".py", "clean"),
    ("x = 1  # see #42\n", ".py", "clean"),
    ("x = 1  # end exclusive\n", ".py", "clean"),

    # --- zero coherence: informative, never noise -----------------------
    ("# callers holding a stale handle crash on the second pass\n"
     "def flush():\n    pass\n", ".py", "clean"),

    # --- clean: comments that carry information -------------------------
    ("# vulture exits 1 both for 'found dead code' and for 'error'\nx = 1\n",
     ".py", "clean"),
    ("# workaround for a driver bug on the 5.4 kernel\nx = 1\n", ".py", "clean"),
    ("x = 1  # noqa: E501\n", ".py", "clean"),
    ("# Copyright 2026 Acme\nx = 1\n", ".py", "clean"),
]
```

- [ ] **Step 2: Написать раннер точности**

```python
from fixtures.comment_corpus import CORPUS  # noqa: E402

# Below this, a class is reported in evidence and never summed into the metric.
MIN_PRECISION = 0.85


class CorpusPrecisionTests(unittest.TestCase):
    def _verdict(self, source, ext):
        got = CM.classify_file(source, ext)
        if got is None:
            return "unreadable"
        for klass in ("commented_out", "trivial_doc", "short_inline"):
            bucket = got["evidence"].get(klass)
            if bucket and bucket["count"]:
                return klass
        return "clean"

    def test_every_row_is_classified_as_labelled(self):
        wrong = []
        for source, ext, expected in CORPUS:
            got = self._verdict(source, ext)
            if got != expected:
                wrong.append((source.splitlines()[0], expected, got))
        self.assertEqual(wrong, [], "misclassified rows: %r" % (wrong,))

    def test_counted_classes_reach_the_precision_bar(self):
        for klass in CM.COUNTED_CLASSES:
            fired = [(s, e, x) for s, e, x in CORPUS if self._verdict(s, e) == klass]
            self.assertTrue(fired, "%s never fired on the corpus" % klass)
            right = sum(1 for _, _, x in fired if x == klass)
            precision = right / len(fired)
            with self.subTest(klass=klass):
                self.assertGreaterEqual(
                    precision, MIN_PRECISION,
                    "%s precision %.2f is below the bar; move it out of "
                    "COUNTED_CLASSES rather than loosening the bar"
                    % (klass, precision))
```

- [ ] **Step 3: Прогнать**

Run: `python3 -m unittest discover -s tests -t tests -k CorpusPrecision -v`
Expected: сначала FAIL со списком misclassified rows. Чинить `comments.py`, а не корпус: корпус — спецификация. Ослаблять `MIN_PRECISION` запрещено; класс, не дотянувший до 0.85, убирается из `COUNTED_CLASSES`.

- [ ] **Step 4: Прогнать весь набор тестов**

Run: `python3 -m unittest discover -s tests -t tests`
Expected: PASS, все тесты, включая 221 существующий

- [ ] **Step 5: Коммит**

```bash
git add tests/fixtures/comment_corpus.py tests/test_comments.py scripts/code_health/comments.py
git commit -m "test(code-health): labelled comment corpus that measures per-class precision"
```

---

### Task 8: Сборка слоя `comments`

**Files:**
- Modify: `scripts/code_health/comments.py`
- Test: `tests/test_comments.py`

**Interfaces:**
- Consumes: `classify_file`; `inventory.source` из `discovery.enumerate_files`
- Produces: `measure_comments(repo_root, inventory, cfg) -> dict` со слотами `status`, `reason`, `files`, `languages_measured`, `not_measured_files`

- [ ] **Step 1: Написать падающий тест**

```python
import tempfile
from pathlib import Path


class _Inventory:
    def __init__(self, source, tests=()):
        self.source = list(source)
        self.tests = list(tests)


class LayerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.cfg = {"chars_per_token": 4}

    def _write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return rel

    def test_measured_layer_lists_only_files_with_a_reader(self):
        self._write("a.py", "# print(x)\ny = 1\n")
        self._write("b.lua", "-- x\n")
        layer = CM.measure_comments(self.root, _Inventory(["a.py", "b.lua"]), self.cfg)
        self.assertEqual(layer["status"], "partial")
        self.assertEqual([row["path"] for row in layer["files"]], ["a.py"])
        self.assertEqual(layer["not_measured_files"], 1)

    def test_all_files_readable_gives_measured(self):
        self._write("a.py", "x = 1\n")
        layer = CM.measure_comments(self.root, _Inventory(["a.py"]), self.cfg)
        self.assertEqual(layer["status"], "measured")
        self.assertEqual(layer["not_measured_files"], 0)

    def test_no_readable_file_gives_not_measured_with_a_reason(self):
        self._write("b.lua", "-- x\n")
        layer = CM.measure_comments(self.root, _Inventory(["b.lua"]), self.cfg)
        self.assertEqual(layer["status"], "not_measured")
        self.assertIn("reader", layer["reason"])
        self.assertEqual(layer["files"], [])

    def test_languages_measured_is_reported(self):
        self._write("a.py", "x = 1\n")
        self._write("b.ts", "let x = 1;\n")
        layer = CM.measure_comments(self.root, _Inventory(["a.py", "b.ts"]), self.cfg)
        self.assertEqual(layer["languages_measured"], ["brace", "python"])

    def test_unreadable_file_is_skipped_without_raising(self):
        layer = CM.measure_comments(self.root, _Inventory(["missing.py"]), self.cfg)
        self.assertEqual(layer["files"], [])
```

- [ ] **Step 2: Прогнать и убедиться, что падает**

Run: `python3 -m unittest discover -s tests -t tests -k LayerTests -v`
Expected: FAIL, `AttributeError: ... has no attribute 'measure_comments'`

- [ ] **Step 3: Реализовать**

```python
import os


def measure_comments(repo_root, inventory, cfg):
    """The layers.comments object. Always returns a status; never raises."""
    rows, langs, unread = [], set(), 0
    chars_per_token = cfg["chars_per_token"]
    for rel in inventory.source:
        ext = os.path.splitext(rel)[1].lower()
        if ext != ".py" and ext not in BRACE_EXT:
            unread += 1
            continue
        try:
            text = (repo_root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            unread += 1
            continue
        got = classify_file(text, ext, chars_per_token)
        if got is None:
            unread += 1
            continue
        langs.add("python" if ext == ".py" else "brace")
        rows.append(dict(path=rel, **got))

    layer = {"files": rows, "languages_measured": sorted(langs),
             "not_measured_files": unread, "reason": None}
    if not rows:
        layer["status"] = "not_measured"
        layer["reason"] = "no source file has a verified comment reader"
    elif unread:
        layer["status"] = "partial"
        layer["reason"] = "%d file(s) have no verified comment reader" % unread
    else:
        layer["status"] = "measured"
    return layer
```

- [ ] **Step 4: Прогнать и убедиться, что проходит**

Run: `python3 -m unittest discover -s tests -t tests -k LayerTests -v`
Expected: PASS, 5 тестов

- [ ] **Step 5: Коммит**

```bash
git add scripts/code_health/comments.py tests/test_comments.py
git commit -m "feat(code-health): comments layer with honest partial degradation"
```

---

### Task 9: Калибровка порогов

**Files:**
- Modify: `docs/superpowers/specs/2026-09-09-comment-noise-design.md` (раздел «Пороги»)

Порогов до этой задачи не существует. Назначать их раньше — то же, что взять «оптимальная плотность 30–60%» из блогов, которое не возводится ни к одному первоисточнику.

- [ ] **Step 1: Спросить пользователя, на каких репозиториях калибровать**

Нужно 3–5 репозиториев с реальной историей. Этот плагин идёт первым как контроль: его комментарии писались вручную и почти все содержат «почему», поэтому здоровый порог обязан оставить его чистым.

- [ ] **Step 2: Собрать распределение**

```bash
for repo in <repo1> <repo2> <repo3>; do
  python3 scripts/measure.py --repo "$repo" --layers comments \
    --out "/tmp/comments-$(basename "$repo").json"
done
```

- [ ] **Step 3: Посчитать перцентили**

```python
import json, statistics, sys

for path in sys.argv[1:]:
    rows = json.load(open(path))["layers"]["comments"]["files"]
    vals = sorted(r["noise_comment_lines"] for r in rows)
    hot = [v for v in vals if v]
    print(path, "files", len(vals), "with noise", len(hot),
          "p50", statistics.median(vals),
          "p90", vals[int(len(vals) * 0.90)],
          "p99", vals[int(len(vals) * 0.99)],
          "max", vals[-1])
```

- [ ] **Step 4: Выбрать числа и записать обоснование**

`warn` = p90 по объединённой выборке, `hard` = p99. Записать в раздел «Пороги» спеки: выбранные числа, репозитории, на которых сняты, и число файлов, которые они помечают. Так же записаны обоснования `parameters.hard = 8` и `near_duplicate.min_similarity = 0.60` в `DESIGN.md:90-92, 1104-1106`.

Если этот плагин при выбранном `warn` даёт хоть одну находку — разобрать каждую вручную. Находка на его собственном коде означает либо дефект детектора, либо реально мусорный комментарий; оба случая чинятся до перехода к Task 10.

- [ ] **Step 5: Коммит**

```bash
git add docs/superpowers/specs/2026-09-09-comment-noise-design.md
git commit -m "docs(code-health): calibrated noise_comment_lines thresholds"
```

---

### Task 10: Встраивание в `measure.py`

**Files:**
- Modify: `scripts/code_health/config.py:18-26`
- Modify: `scripts/code_health/findings.py:12`, `:27-35`, новая функция после `size_findings`
- Modify: `scripts/measure.py:17-25`, `:71-95`
- Test: `tests/test_findings.py`, `tests/test_config.py:34-47`, `tests/test_measure.py:40-49`

**Interfaces:**
- Consumes: `measure_comments` из Task 8; числа из Task 9
- Produces: находка `comment_noise` с `metric: "noise_comment_lines"`, `layer: "L2"`, `subject: {"file": ...}`

- [ ] **Step 1: Написать падающий тест**

```python
class CommentFindingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = C.load_config(Path(tempfile.mkdtemp()))

    def test_value_at_warn_fires_warn(self):
        warn = self.cfg["thresholds"]["noise_comment_lines"]["warn"]
        layer = {"files": [{"path": "a.py", "noise_comment_lines": warn,
                            "evidence": {"tokens": 40}}]}
        out = F.comment_findings(layer, self.cfg)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["level"], "warn")
        self.assertEqual(out[0]["id"], "comment_noise:a.py")
        self.assertEqual(out[0]["metric"], "noise_comment_lines")

    def test_one_below_warn_fires_nothing(self):
        warn = self.cfg["thresholds"]["noise_comment_lines"]["warn"]
        layer = {"files": [{"path": "a.py", "noise_comment_lines": warn - 1,
                            "evidence": {}}]}
        self.assertEqual(F.comment_findings(layer, self.cfg), [])

    def test_none_never_reaches_the_comparison(self):
        layer = {"files": [{"path": "a.py", "noise_comment_lines": None,
                            "evidence": {}}]}
        self.assertEqual(F.comment_findings(layer, self.cfg), [])

    def test_comments_layer_needs_no_external_tool(self):
        self.assertEqual(F.tools_for({"comments"}), [])
```

Плюс две правки в существующих тестах.

В `tests/test_config.py`, в словарь `expected` теста
`test_every_documented_threshold_is_present` (числа — из Task 9):

```python
            "noise_comment_lines": (<calibrated warn>, <calibrated hard>),
```

В `tests/test_measure.py`, в `RESULT_DEPS`:

```python
    "comments": {"files": ()},
```

Без второй правки новый слой не проверяется контрактом «`measured` с пустым
результатом при неизмеренной зависимости».

- [ ] **Step 2: Прогнать и убедиться, что падает**

Run: `python3 -m unittest discover -s tests -t tests -k "CommentFinding or Config or Measure" -v`
Expected: FAIL, `KeyError: 'noise_comment_lines'`

- [ ] **Step 3: Реализовать**

В `scripts/code_health/config.py`, внутри `DEFAULTS["thresholds"]`, после `"nesting"` (числа — из Task 9):

```python
        "noise_comment_lines": {"warn": <calibrated warn>, "hard": <calibrated hard>},
```

В `scripts/code_health/findings.py`:

```python
LAYER_NAMES = ("size", "dup", "imports", "dead", "churn", "derived", "comments")
```

`parse_args` строит `--layers` по умолчанию как `",".join(F.LAYER_NAMES)`
(`scripts/measure.py:50`), поэтому слой начинает работать в каждом запуске без
флага. Он читает каждый исходный файл второй раз — `sizes.measure_files` уже
прочитал их для подсчёта символов. Второе чтение с диска на порядки дешевле
подпроцесса lizard'а и остаётся так; если профиль на большом репозитории скажет
иначе, чтение надо разделить, а не отключать слой.

и в `LAYER_TOOLS`:

```python
    # Reads files directly with the stdlib; no external tool exists for it.
    "comments": (),
```

и новая функция после `size_findings`:

```python
def comment_findings(layer, cfg):
    threshold = cfg["thresholds"]["noise_comment_lines"]
    out = []
    for row in layer.get("files", []):
        value = row.get("noise_comment_lines")
        if value is None:
            continue
        level = _config.level_for(value, threshold)
        if level is None:
            continue
        out.append(_finding("comment_noise", level, {"file": row["path"]},
                            Measurement("noise_comment_lines", value,
                                        threshold[level]),
                            row.get("evidence", {})))
    out.sort(key=lambda f: f["id"])
    return out
```

В `scripts/measure.py`, к списку импортов:

```python
from code_health import comments as CM   # noqa: E402
```

и ветка после блока `derived`:

```python
    if "comments" in wanted:
        layers["comments"] = CM.measure_comments(repo_root, inventory, cfg)
        all_findings.extend(F.comment_findings(layers["comments"], cfg))
```

- [ ] **Step 4: Прогнать весь набор**

Run: `python3 -m unittest discover -s tests -t tests`
Expected: PASS

Затем прогнать измеритель на самом плагине:

Run: `python3 scripts/measure.py --repo . --layers comments --out /tmp/self.json && python3 -c "import json; d=json.load(open('/tmp/self.json')); print(d['layers']['comments']['status'], len(d['findings']))"`
Expected: `measured 0` или `partial 0`. Находка на собственном коде плагина разбирается вручную до коммита.

- [ ] **Step 5: Коммит**

```bash
git add scripts/code_health/config.py scripts/code_health/findings.py \
        scripts/measure.py tests/test_findings.py tests/test_config.py \
        tests/test_measure.py
git commit -m "feat(code-health): comment_noise findings in the measure document"
```

---

### Task 11: Шестое anti-gaming правило и группировка в аудите

**Files:**
- Modify: `skills/fix/SKILL.md:187-201`
- Modify: `skills/audit/SKILL.md:184-193`

**Interfaces:**
- Consumes: `evidence.<class>.lines` из находки `comment_noise`

Существующие пять правил вырезание комментариев не ловят: тесты целы, конфиги не тронуты, файлы не удалены. Правило 5 сравнивает repo-wide `summary`, а он при удалении комментариев улучшается по `file_lines` и `file_tokens` — то есть подтверждает ложное улучшение.

- [ ] **Step 1: Добавить правило в `skills/fix/SKILL.md`**

Шестым пунктом в списке шага 4:

```markdown
6. Comment lines removed by the diff that the group's finding did not list as
   noise. The finding carries the exact line numbers in
   `evidence.<class>.lines`; anything outside that set is a comment the
   detector judged worth keeping, and deleting it is how this metric is gamed.
   Removing comments improves `file_lines` and `file_tokens`, so rule 5 reads
   the deletion as an improvement and will not catch it.
```

- [ ] **Step 2: Добавить строку в таблицу группировки `skills/audit/SKILL.md`**

```markdown
| `comment_noise` | subsystem/directory | "31 files carrying commented-out code under `legacy/`" — capped at `P2` |
```

- [ ] **Step 3: Описать `zero_coherence` как re-ranking evidence в `skills/audit/SKILL.md`**

Сразу под таблицей группировки:

```markdown
A `comment_noise` finding's `evidence.zero_coherence` lists comments that share
no word with the function they document. That is not a finding of its own and
never becomes a group. When such a function also carries a
`function_too_complex`, `function_too_long` or `too_deep_nesting` finding, name
the comment in that group's `why` as evidence: the code needed prose to be
understood, which is the group's own argument. When the function carries no
other finding, ignore the entry.
```

Правило совпадает с уже действующим для `hotspot`, `edit_fanout` и
`symbol_collision`: группу из одних re-ranking-находок создавать нельзя
(`references/ledger.md:79-95`).

- [ ] **Step 4: Проверить ссылки**

Run: `python3 -m unittest discover -s tests -t tests -k References -v`
Expected: PASS

- [ ] **Step 5: Коммит**

```bash
git add skills/fix/SKILL.md skills/audit/SKILL.md
git commit -m "feat(code-health): sixth anti-gaming rule for deleted comments"
```

---

### Task 12: Документация

**Files:**
- Modify: `references/metrics.md:18-26`, `:62-81`, `:83-96`
- Modify: `references/quality-policy.md:141-148`
- Modify: `DESIGN.md` §4, §5, §8
- Modify: `README.md`, `skills/audit/SKILL.md:3`

- [ ] **Step 1: Строка в таблице порогов `references/metrics.md`**

Колонка «How it hurts an agent» обязательна — это фактический контракт таблицы.

```markdown
| Noise comment lines | `noise_comment_lines` | lines | <warn> | <hard> | L2, this plugin's own reader | Every line of a comment that repeats the code next to it is context the next agent pays for and learns nothing from. |
```

- [ ] **Step 2: Строка в таблице «Finding kind → `--layers` value»**

```markdown
| `comment_noise` | `comments` |
```

Без неё `fix` шаг 3.1 не сможет ревалидировать группу.

- [ ] **Step 3: Потолок P2 в `references/quality-policy.md`**

```markdown
The `trivial_doc` detector is measured at ~0.90 and `commented_out` at ~0.89,
so a `comment_noise` group is capped at **P2** for the same reason
`dead_symbol` is: a verdict at that precision is evidence to inspect, not
proof. The report also states what the detector cannot see — a comment that
paraphrases the code with different words needs a language model to catch,
and this layer does not run one.
```

- [ ] **Step 4: `DESIGN.md`**

§4 — строка в таблице порогов. §5 — как измеряется: три детектора, иммунитет, языки, `commented_out` только для Python. §8 — правило группировки. Плюс закрыть уже записанный долг в `DESIGN.md:371-373`: near-duplicate по-прежнему не стрипает комментарии, и теперь в репозитории есть ридер, который это умеет — отметить как возможность, не реализовывать.

- [ ] **Step 5: Перечисление метрик**

В `README.md` и `skills/audit/SKILL.md:3` строка «size, tokens, complexity, parameters, nesting, near-duplicates, import cycles, dead code, churn» получает `comment noise`.

- [ ] **Step 6: Прогнать весь набор и закоммитить**

```bash
python3 -m unittest discover -s tests -t tests
git add references/ DESIGN.md README.md skills/audit/SKILL.md
git commit -m "docs(code-health): document the comment_noise metric and its blind spot"
```
