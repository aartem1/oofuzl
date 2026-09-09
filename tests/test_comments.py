import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import load_package

load_package()
from code_health import comments as CM  # noqa: E402
from comment_corpus import CORPUS  # noqa: E402


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

    def test_tokenize_failure_returns_none_not_empty(self):
        # `def (:` has an unclosed parenthesis, so `tokenize` itself raises
        # TokenError -- this input never reaches `ast.parse`.
        self.assertIsNone(CM.extract_python("def (:\n"))

    def test_source_that_tokenizes_but_does_not_parse_returns_none(self):
        """`tokenize` is lexical and succeeds where `ast.parse` fails.

        Verified staging for `x = = 1`: `tokenize` returns a full token
        stream, `ast.parse` raises SyntaxError. Without this, the `#`
        comments would come back while the docstrings were never extracted
        and the declaration table was never built -- a measured zero for a
        file nothing here could read. The everyday trigger is not a typo but
        syntax newer than the interpreter running the audit (PEP 695
        `type X = int` on 3.11, `match` on 3.9).
        """
        src = '# print(x)\ndef f():\n    """Doc."""\n    x = = 1\n'
        self.assertIsNone(CM.extract_python(src))
        self.assertIsNone(CM.extract_python("a b c\n"))


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


class ImmunityTests(unittest.TestCase):
    """The two immunity passes, pinned apart.

    `is_immune` -- the union of both -- had no production caller and has been
    retired; asserting through it hid which half each text belongs to, which
    is the whole point of the split Task 4 made. `is_directive` gates every
    detector including `commented_out`; `is_prose_immune` gates only the prose
    detectors, so a commented-out `# return None` is a return statement rather
    than documentation of null semantics.
    """

    DIRECTIVE = [
        "noqa: E501",
        "type: ignore[arg-type]",
        "eslint-disable-next-line no-console",
        "TODO: drop this fallback",
        "FIXME later",
        "Copyright 2026 Acme",
        "SPDX-License-Identifier: MIT",
        "see #1234 for the original report",
        "see https://bugs.example/1 for why",
        "see RAM-436",
        "-*- coding: utf-8 -*-",
        "!/usr/bin/env python3",
    ]
    PROSE = [
        "ms, not seconds",
        "end is exclusive",
        "returns None when the cache is cold",
        "the caller owns the handle and must close it",
        "workaround for a driver bug",
        "иначе счётчик сбрасывается на втором проходе",
        " ".join(["word"] * 30),
    ]
    NOT_IMMUNE = [
        "increment counter",
        "loop through the items",
        "removes all defined markers",
        "set x to 1",
        "empty the cache before returning",
        "closes the file handle",
        "none of the retries succeeded",
        "reads utf-8 from disk",
        " ".join(["word"] * 29),
    ]

    def test_directive_texts_are_directives(self):
        for text in self.DIRECTIVE:
            with self.subTest(text=text):
                self.assertTrue(CM.is_directive(text))

    def test_prose_texts_are_prose_immune(self):
        for text in self.PROSE:
            with self.subTest(text=text):
                self.assertTrue(CM.is_prose_immune(text))

    def test_prose_immunity_does_not_leak_into_the_directive_half(self):
        """The split only bites if the two lists are actually disjoint.

        A prose text matching `is_directive` would still gate
        `commented_out`, which is exactly what the split exists to prevent.
        """
        for text in self.PROSE:
            with self.subTest(text=text):
                self.assertFalse(CM.is_directive(text))

    def test_not_immune_texts_match_neither_pass(self):
        for text in self.NOT_IMMUNE:
            with self.subTest(text=text):
                self.assertFalse(CM.is_directive(text))
                self.assertFalse(CM.is_prose_immune(text))

    def test_the_union_helper_is_gone(self):
        # `is_immune` had no production caller; only its own tests used it,
        # so it pinned nothing that `classify_file` actually runs.
        self.assertFalse(hasattr(CM, "is_immune"))


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
        # Real deprecated annotation, xhh (Fix 3): a valueless AnnAssign whose
        # annotation is a type expression (Subscript), not a bare Constant.
        "clusters: dict[str, Any]",
        # Real commented-out call, xhh (Fix 4): no whitespace before the "(".
        "put_oauth_error_state(self, HTTPStatus.FORBIDDEN)",
        # Fix 3 round 2: every type-expression shape the annotation
        # discriminator must accept.
        "x: int",
        'name: str = "a"',
        "items: list[int]",
        "cfg: typing.Optional[Config]",
        "value: int | None",
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
        # /proc/meminfo parser field-format note (Fix 3): a valueless
        # AnnAssign whose annotation is a bare string Constant, not a type.
        'Format: "MemAvailable:   1234567 kB"',
        # Metrics-direction note (Fix 4): a call with a space before "(".
        "lower_is_better (default)",
        # except-handler comment, jira-resource-allocation (Fix 5): "continue"
        # inside "continue-on-error" is not the `continue` statement keyword.
        "continue-on-error (CancelledError propagates",
        # BRAND tuple's inline comment, jira-resource-allocation (Fix 2): the
        # nested '#' before "D6001C" opens a Python comment, truncating the
        # parse to nothing.
        "#D6001C — HH brand red (service.hh), primary accent",
        "data,",
        "x",
        "the parser",
        "C/Java/C# typed form (, ).",
        # Country:Capital label comment, real text, xhh/utils/
        # employerview_meta_tags.py (Fix 3 round 2): a bare Name annotation
        # only reads as a type when the name is a known builtin/typing type;
        # Cyrillic identifiers are legal Python 3 names but not types.
        "Казахстан:Астана",
        "Note: see the ticket",
        "Warning: this is slow",
    ]

    def test_code_is_detected(self):
        for text in self.CODE:
            with self.subTest(text=text):
                self.assertTrue(CM.is_commented_out(text))

    def test_prose_is_not_detected(self):
        for text in self.NOT_CODE:
            with self.subTest(text=text):
                self.assertFalse(CM.is_commented_out(text))

    def test_pathological_input_reaches_the_parser_and_does_not_raise(self):
        """CPython's parser raises MemoryError on a long unary-operator run.

        This test used to pass `"-" * 50000 + "1"`, which contains none of
        `_CODE_INDICATORS` and no code word, so `_line_is_code` returned False
        before `ast.parse` was ever called -- the handler the test names was
        never reached. Instrumented: 0 calls to `ast.parse` for that input, 1
        for the one below, which carries an `=` and so gets past the indicator
        gate. The assertion on `seen` is what keeps it reaching the parser.
        """
        pathological = "x = " + "-" * 50000 + "1"
        seen = []
        real = CM.ast.parse

        def spy(*args, **kwargs):
            seen.append(args[0])
            return real(*args, **kwargs)

        with mock.patch.object(CM.ast, "parse", spy):
            result = CM.is_commented_out(pathological)
        self.assertEqual(len(seen), 1, "the input never reached ast.parse")
        self.assertFalse(result)

    def test_cyrillic_identifier_does_not_shift_the_space_check(self):
        """`end_col_offset` is a UTF-8 byte offset, not a character index.

        Verified: `кэш(x, y)` reports `end_col_offset=6` against a
        9-character, 12-byte line. Indexed by character that lands on the
        space inside the argument list, so the call read as prose and the
        line was missed; indexed by byte it lands on the `(`. The spaced form
        must still be rejected, or the fix would only have moved the bug.
        """
        self.assertTrue(CM.is_commented_out("кэш(x, y)"))
        self.assertFalse(CM.is_commented_out("кэш (x, y)"))
        self.assertTrue(CM.is_commented_out("получить_кэш(ключ, значение)"))
        self.assertFalse(CM.is_commented_out("получить_кэш (ключ, значение)"))

    def test_refuses_directive_text_on_its_own_regardless_of_caller_order(self):
        """`is_commented_out` carries its own internal `is_directive` guard.

        This was checked directly against `classify_file` by swapping
        `is_directive` and `is_commented_out` there: the verdict for a TODO
        whose body also parses as code (`x = 1`) did not change either way,
        because `is_commented_out` refuses directive text before it ever
        looks for code. That redundancy is deliberate defence in depth --
        directive metadata must never read as code even if some future
        caller invokes this function without gating on `is_directive` first
        -- so this test pins the function's own behaviour, not an ordering
        in `classify_file` (contrast `GateOrderTests`, whose three cases
        each fail if their two named gates are swapped there).
        """
        self.assertFalse(CM.is_commented_out("TODO: x = 1"))

    def test_docstring_with_code_example_is_not_flagged(self):
        """Regression: the docstring of _if_depth in pyast.py, line 222.

        One code-looking line (`else:`) in a 16-line prose block should not
        trigger the detector. Majority rule: > half of non-empty lines must
        read as code. Real comment: 1 code line of 16 non-empty lines fails.
        """
        # Verbatim from scripts/code_health/pyast.py lines 222-236
        docstring = """One `if`/`elif`/`else` chain is one nesting level, not one per branch.

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
    grammar, not a gap in this pass."""
        self.assertFalse(CM.is_commented_out(docstring))

    def test_bare_expression_without_call_is_not_flagged(self):
        """Regression: a real comment from comments.py line 374.

        Prose that parses as valid Python syntax but contains no function
        calls is documentation, not commented-out code. This line parses as
        a valid binary expression (C/Java slash C# slash form) but the # in
        C# truncates it for compile(). After compile succeeds, it has no Call.
        """
        # Verbatim from scripts/code_health/comments.py line 374
        prose_that_parses = "C/Java/C# typed form (`static int read_block(`, `static char *read_line(`)."
        self.assertFalse(CM.is_commented_out(prose_that_parses))


class DocstringGateTests(unittest.TestCase):
    """Fix 1: `commented_out` must never run on a docstring.

    eradicate's own algorithm only ever examines a `#` line comment;
    extending it to docstrings was the design error behind every live
    `commented_out` false positive found against three real repositories
    (Task 9) -- a JSON schema worked example in `xhh/ajax.py`, a payload
    tree in `reports_quarterly_summary.py`, and `feature_id:`/`code_owner:`
    metadata in `resume-profile-front` all read as majority-code line by
    line while being documentation.
    """

    def test_docstring_record_is_marked_as_such(self):
        got = CM.extract_python('def f():\n    """Return f."""\n    return 1\n')
        record = next(c for c in got if c.text == "Return f.")
        self.assertTrue(record.docstring)

    def test_ordinary_comment_is_not_marked_as_a_docstring(self):
        got = CM.extract_python("# set up\nx = 1\n")
        self.assertFalse(got[0].docstring)

    def test_docstring_with_a_schema_example_never_reaches_commented_out(self):
        """Shape of the xhh/ajax.py and resume-profile-front false
        positives: quoted, indented keys outnumber the surrounding prose
        5-to-2 and used to fire the majority rule on the whole docstring.
        """
        src = ('def handler():\n'
               '    """Request payload.\n\n'
               '    {\n'
               '        "feature_id": 12,\n'
               '        "code_owner": "team-x",\n'
               '        "status": "active",\n'
               '        "priority": 3,\n'
               '    }\n'
               '    """\n'
               '    return {}\n')
        got = CM.classify_file(src, ".py")
        self.assertEqual(got["evidence"]["commented_out"]["count"], 0)

    def test_trivial_doc_still_fires_on_a_docstring(self):
        """`trivial_doc` is a docstring's main home; the gate above must not
        block it -- Steidl's own worked example (`removeAllMarkers`).
        """
        src = 'def remove_all_markers():\n    """Removes all markers."""\n    pass\n'
        got = CM.classify_file(src, ".py")
        self.assertEqual(got["evidence"]["trivial_doc"]["lines"], [2])
        self.assertEqual(got["evidence"]["commented_out"]["count"], 0)


class DocstringOwnerTests(unittest.TestCase):
    """A docstring binds to the declaration above it, never the one below.

    `classify_file` used to resolve every comment's owner as
    `decls.get(end_line + 1) or decls.get(line - 1)` -- following declaration
    first. That is right for a `#` comment above a `def` and wrong for a
    docstring, whose owner is unambiguous: the declaration it is nested in.
    """

    def test_docstring_record_carries_its_owning_declaration(self):
        got = CM.extract_python('def f():\n    """Return f."""\n    return 1\n')
        record = next(c for c in got if c.text == "Return f.")
        self.assertEqual(record.owner, "f")

    def test_a_hash_comment_carries_no_owner(self):
        got = CM.extract_python("# set up\nx = 1\n")
        self.assertIsNone(got[0].owner)

    def test_a_module_docstring_has_no_owner(self):
        got = CM.extract_python('"""Load all markers."""\n'
                                'def load_all_markers():\n    pass\n')
        record = next(c for c in got if c.docstring)
        self.assertIsNone(record.owner)

    def test_factory_docstring_is_scored_against_the_factory(self):
        """Verified false positive: this scored 1.00 against the nested
        function and 0.33 against its real owner, and was counted.
        A factory or decorator whose docstring names what it builds is
        idiomatic Python.
        """
        src = ('def build_handler():\n'
               '    """Make the handler."""\n'
               '    def make_the_handler():\n'
               '        pass\n')
        got = CM.classify_file(src, ".py")
        self.assertEqual(got["evidence"]["trivial_doc"]["count"], 0)
        self.assertEqual(got["noise_comment_lines"], 0)

    def test_module_docstring_is_not_scored_against_the_first_declaration(self):
        src = ('"""Load all markers."""\n'
               'def load_all_markers():\n    pass\n')
        got = CM.classify_file(src, ".py")
        self.assertEqual(got["evidence"]["trivial_doc"]["count"], 0)

    def test_a_docstring_restating_its_own_declaration_still_fires(self):
        # The fix must not cost the class its main case: Steidl's own worked
        # example, and the same shape on a class.
        for src in ('def remove_all_markers():\n'
                    '    """Removes all markers."""\n    pass\n',
                    'class UserCache:\n    """User cache."""\n    pass\n'):
            with self.subTest(src=src.splitlines()[0]):
                got = CM.classify_file(src, ".py")
                self.assertEqual(got["evidence"]["trivial_doc"]["lines"], [2])

    def test_a_hash_comment_still_binds_to_the_declaration_below_it(self):
        got = CM.classify_file("# increment counter\n"
                               "def increment_counter():\n    pass\n", ".py")
        self.assertEqual(got["evidence"]["trivial_doc"]["lines"], [1])


class CoherenceTests(unittest.TestCase):
    def test_identifier_splits_on_camel_and_snake(self):
        self.assertEqual(CM.split_identifier("removeAllMarkers"),
                         ["remove", "all", "markers"])
        self.assertEqual(CM.split_identifier("remove_all_markers"),
                         ["remove", "all", "markers"])
        self.assertEqual(CM.split_identifier("HTTPServerError"),
                         ["http", "server", "error"])

    def test_digit_stays_with_the_preceding_segment(self):
        self.assertEqual(CM.split_identifier("name2Value"), ["name2", "value"])

    def test_paper_example_scores_three_quarters(self):
        # Steidl, ICPC 2013: this exact pair is the paper's worked example.
        got = CM.coherence("removes all defined markers", "removeAllMarkers")
        self.assertAlmostEqual(got, 0.75)

    def test_plural_matches_within_levenshtein_one(self):
        self.assertAlmostEqual(CM.coherence("markers", "marker"), 1.0)

    def test_two_edits_apart_do_not_match(self):
        # True Levenshtein distance 2 ("rk" -> "rk" then "e"/"u", "r"/"p").
        self.assertAlmostEqual(CM.coherence("marker", "markup"), 0.0)

    def test_three_edits_apart_do_not_match(self):
        self.assertAlmostEqual(CM.coherence("marker", "mask"), 0.0)

    def test_single_character_strings_never_match_unless_identical(self):
        """At length one the edit-distance rule is vacuous: any two distinct
        single characters sit at Levenshtein distance 1, so without a floor
        `_within_one_edit` would match any letter to any other -- including
        a Cyrillic character against an unrelated Latin one.
        """
        self.assertFalse(CM._within_one_edit("и", "a"))
        self.assertTrue(CM._within_one_edit("и", "и"))

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

    def test_go_method_with_receiver(self):
        self.assertEqual(CM.declarations_brace("func (c *Cache) Load() error {\n"),
                         {1: "Load"})

    def test_arrow_function_bound_to_a_name(self):
        self.assertEqual(
            CM.declarations_brace("export const load = async () => {\n"),
            {1: "load"})
        self.assertEqual(CM.declarations_brace("const handler = function(x) {\n"),
                         {1: "handler"})

    def test_pointer_return_with_star_bound_to_the_name(self):
        self.assertEqual(
            CM.declarations_brace("static char *read_line(FILE *f) {\n"),
            {1: "read_line"})

    def test_call_site_is_not_a_declaration(self):
        # Each of these matches the TYPED shape's "token(s), then name, then
        # (" syntax -- only the trailing `;`/`:` check tells a call site
        # apart from a declaration whose brace sits on the same line.
        for line in (
            "    doWork(x);\n",
            "await foo(x);\n",
            "throw makeError(x);\n",
            "new Foo(x);\n",
            "typeof check(x);\n",
            "yield foo(x);\n",
            "case handle(x):\n",
        ):
            with self.subTest(line=line):
                self.assertEqual(CM.declarations_brace(line), {})

    def test_control_flow_parenthesis_is_not_a_declaration(self):
        for line in (
            "    if (ready) {\n",
            "    for (;;) {\n",
            "    while (x) {\n",
            "    switch (x) {\n",
            "    catch (e) {\n",
        ):
            with self.subTest(line=line):
                self.assertEqual(CM.declarations_brace(line), {})

    def test_control_keyword_before_a_bare_call_reaches_the_guard(self):
        # `return factory(x)` (no trailing `;`, as under ASI) matches the
        # TYPED shape structurally -- name "factory", ending in `)`. Only
        # the `_NOT_A_DECL` check on the line's leading token stops this
        # from being read as a declaration; this is the case the previous
        # `if (ready) {` test never reached, because that line fails to
        # match TYPED_DECL at all (nothing precedes its "(").
        for keyword in ("return", "else", "do", "with"):
            line = "    %s factory(x)\n" % keyword
            with self.subTest(keyword=keyword):
                self.assertEqual(CM.declarations_brace(line), {})

    def test_prototype_without_a_body_is_a_deliberate_miss(self):
        # `int f(void);` ends in `;`, indistinguishable on this evidence from
        # `throw makeError(x);` above -- rejecting both is the accepted cost
        # of rejecting the call-site false positives.
        self.assertEqual(CM.declarations_brace("int f(void);\n"), {})

    def test_call_site_with_trailing_block_comment_is_not_a_declaration(self):
        # The `;` that condemns this line as a statement sits before a
        # trailing `/* note */`, not before end of line -- the block comment
        # must be stripped too, the same as a trailing `//` comment is.
        self.assertEqual(
            CM.declarations_brace("throw makeError(x); /* note */\n"), {})

    def test_declaration_with_trailing_block_comment_still_matches(self):
        self.assertEqual(
            CM.declarations_brace("static int read_block(int fd) { /* sets up */\n"),
            {1: "read_block"})

    def test_unterminated_block_comment_is_treated_as_a_statement(self):
        # `/* note` with no closing `*/` on this line hides text we cannot
        # see -- it might continue as a comment, or the `*/` and real code
        # might be on the next line. Guessing risks reintroducing the false
        # positive this class exists to reject, so the unresolved case is
        # treated as a statement: rejecting a real declaration here is
        # cheaper than misattributing a comment to a call site's name.
        self.assertEqual(
            CM.declarations_brace("throw makeError(x); /* note\n"), {})


class BraceLanguageProbeTests(unittest.TestCase):
    """Every extension in `BRACE_EXT` must resolve a real declaration.

    `BRACE_EXT` decides which files get `status: "measured"` and a
    `trivial_doc` count. An extension whose declarations `declarations_brace`
    cannot see still yields comments, so every one of its files reports
    `trivial_doc: count 0` -- "checked, found nothing" for a language nobody
    checked. That is the same silent zero this repository refuses for
    lizard's nesting column, one level down, so admission to `BRACE_EXT` is
    pinned here by a per-extension probe rather than by the reader existing.
    The table and the rejected extensions are recorded in
    `references/tools.md`.
    """

    # (extension, one realistic declaration, the name it must resolve to)
    PROBE = [
        (".c", "static int read_block(int fd) {", "read_block"),
        (".h", "static inline int cache_size(void) {", "cache_size"),
        (".cpp", "int Cache::load_all(int fd) {", "load_all"),
        (".cc", "std::string Cache::name() const {", "name"),
        (".hpp", "class Cache {", "Cache"),
        (".cs", "public async Task<int> LoadAll(int fd) {", "LoadAll"),
        (".java", "public double returnTotalPrice() {", "returnTotalPrice"),
        (".js", "function returnTotalPrice() {", "returnTotalPrice"),
        (".jsx", "export function UserRow(props) {", "UserRow"),
        (".mjs", "export async function loadAll() {", "loadAll"),
        (".cjs", "const load = function(x) {", "load"),
        (".ts", "export interface FiltersState {", "FiltersState"),
        (".tsx", "export const UserRow = (props: Props) => {", "UserRow"),
        (".go", "func (c *Cache) LoadAll() []Entry {", "LoadAll"),
        (".kt", "fun loadAll(fd: Int): Int {", "loadAll"),
        (".rs", "pub fn parse_all(x: u8) -> u8 {", "parse_all"),
        (".swift", "func loadAll(fd: Int) -> Int {", "loadAll"),
        (".scala", "def loadAll(fd: Int): Int = {", "loadAll"),
    ]

    def test_every_probed_extension_resolves_its_declaration(self):
        for ext, line, name in self.PROBE:
            with self.subTest(ext=ext):
                self.assertEqual(CM.declarations(line + "\n", ext), {1: name})

    def test_the_probe_covers_every_extension_in_brace_ext(self):
        """No extension may join `BRACE_EXT` without a probe row.

        Without this, the next extension added silently inherits `measured`
        on the strength of the comment scanner alone, which is exactly how
        `.m` and `.mm` got in.
        """
        self.assertEqual(sorted(CM.BRACE_EXT),
                         sorted(ext for ext, _, _ in self.PROBE))

    def test_objective_c_stays_out_because_it_fails_the_probe(self):
        """`- (void)m:(int)x {` matches none of the three declaration shapes.

        Kept as a test rather than deleted with the extension: it records
        why `.m`/`.mm` are absent, so re-adding them without a fourth
        declaration shape turns this red instead of restoring a silent zero.
        """
        self.assertEqual(
            CM.declarations_brace("- (void)loadAllMarkers:(int)x {\n"), {})
        self.assertNotIn(".m", CM.BRACE_EXT)
        self.assertNotIn(".mm", CM.BRACE_EXT)

    def test_matlab_source_is_not_read_as_a_brace_language(self):
        """`.m` is MATLAB at least as often as Objective-C.

        MATLAB comments are `%`, invisible to this reader, and its transpose
        operator `'` opens a literal `_skip_literal` never closes -- so the
        rest of the file is swallowed and the file reports zero comments.
        """
        matlab = ("% normalise each column\n"
                  "function y = normalise(x)\n"
                  "  y = x' * 2;  % transpose, then scale\n"
                  "end\n")
        self.assertIsNone(CM.extract(matlab, ".m"))
        self.assertEqual(CM.extract_brace(matlab), [])


class CppQualifiedNameTests(unittest.TestCase):
    """An out-of-line C++ member definition names the method, not nothing.

    `int Cache::load_all(int fd) {` is the ordinary shape of a `.cpp`
    definition whose declaration sits in the header; before the `::` was
    accepted, every such file reported `trivial_doc: 0` regardless of its
    comments.
    """

    def test_qualified_member_definition_resolves_the_bare_name(self):
        self.assertEqual(CM.declarations_brace("int Cache::load_all(int fd) {\n"),
                         {1: "load_all"})

    def test_namespaced_return_type_does_not_block_the_match(self):
        self.assertEqual(
            CM.declarations_brace("std::string Cache::name() const {\n"),
            {1: "name"})

    def test_a_single_colon_is_not_a_qualifier(self):
        # `handler: makeHandler(x),` is a JS object-literal property whose
        # value is a call. Accepting a single `:` in the type position would
        # read it as a declaration of `makeHandler` -- the call-site false
        # positive `_is_statement` cannot catch, because the line ends in a
        # comma rather than a `;`.
        self.assertEqual(
            CM.declarations_brace("  handler: makeHandler(x),\n"), {})


class DeclarationDispatchTests(unittest.TestCase):
    def test_python_routes_to_ast(self):
        self.assertEqual(CM.declarations("def f():\n    pass\n", ".py"), {1: "f"})

    def test_brace_routes_to_regex(self):
        self.assertEqual(CM.declarations("function f() {}\n", ".js"), {1: "f"})

    def test_unknown_extension_has_no_declarations(self):
        self.assertEqual(CM.declarations("function f()\n", ".lua"), {})


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

    def test_python_failing_tokenize_returns_none_not_zero(self):
        self.assertIsNone(CM.classify_file("def (:\n", ".py"))

    def test_python_failing_parse_returns_none_not_zero(self):
        """A file that tokenizes but does not parse is not measured.

        This exact source used to return `noise_comment_lines: 1` with
        `trivial_doc: {"lines": [], "count": 0}` -- the docstring on line 3
        was never extracted and no declaration table was built, yet the file
        joined `layer["files"]` as measured with a zero for a class nobody
        ran.
        """
        src = '# print(x)\ndef f():\n    """Doc."""\n    x = = 1\n'
        self.assertIsNone(CM.classify_file(src, ".py"))

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

    def test_zero_coherence_is_line_ordered(self):
        """`audit` asks a human to read these entries down the file.

        `extract_python` used to return every `#` comment and then every
        docstring, so the list interleaved by kind rather than by position:
        the docstring on line 2 came after the `#` comment on line 5.
        """
        src = ('def alpha():\n'
               '    """Zzz nothing shared here at all."""\n'
               '    pass\n'
               '\n'
               '# unrelated words entirely different\n'
               'def beta():\n'
               '    pass\n'
               '\n'
               'def gamma():\n'
               '    """Another unrelated sentence here."""\n'
               '    pass\n')
        got = CM.classify_file(src, ".py")
        entries = got["evidence"]["zero_coherence"]
        self.assertEqual([e["line"] for e in entries], [2, 5, 10])
        self.assertEqual([e["function"] for e in entries],
                         ["alpha", "beta", "gamma"])

    def test_zero_coherence_never_counts_as_noise(self):
        got = CM.classify_file("# see #42 for the reason\ndef flush():\n    pass\n",
                               ".py")
        self.assertEqual(got["evidence"]["zero_coherence"], [])
        self.assertEqual(got["evidence"]["immune"], 1)


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

    def test_every_counted_class_fires_at_least_once(self):
        """What this class can actually assert, said in its own name.

        There used to be a precision assertion here against a 0.85 bar. It
        could never fail: `test_every_row_is_classified_as_labelled` above
        already asserts that every row lands in the class it is labelled
        with, so precision over this corpus is 1.00 by construction and the
        bar was inert. What is left is the one thing this corpus can
        independently show -- that neither counted class has gone silent,
        which a detector regression would otherwise hide behind a green
        "no misclassified rows".

        The 0.85 floor itself is policy, not a corpus measurement; it lives
        in `references/quality-policy.md`, and the class's live-code
        precision is in `references/metrics.md`.
        """
        for klass in CM.COUNTED_CLASSES:
            fired = [s for s, e, _ in CORPUS if self._verdict(s, e) == klass]
            with self.subTest(klass=klass):
                self.assertTrue(fired, "%s never fired on the corpus" % klass)


class GateOrderTests(unittest.TestCase):
    """Pin the order the classification gates run in, not just the outcome.

    Each of these three cases is chosen so that swapping its two named
    adjacent gates in `classify_file` flips the verdict -- confirmed by
    actually performing that swap for each one; a test that only checks the
    final bucket, without a case shaped like this, would not notice a
    reordering. A fourth candidate case (directive vs. commented_out) was
    removed from here: `is_commented_out` already refuses directive text on
    its own, so no ordering in `classify_file` was actually being pinned by
    it -- see `CommentedOutTests.test_refuses_directive_text_on_its_own_regardless_of_caller_order`.
    """

    def test_commented_out_gate_runs_before_prose_immunity(self):
        """`is_commented_out` must run before `is_prose_immune`.

        `None` is on the prose-immune word list (it documents null
        semantics), but `# return None` is a return statement, not
        documentation -- swapping the gates would make it immune instead.
        """
        got = CM.classify_file("# return None\nx = 1\n", ".py")
        self.assertEqual(got["evidence"]["commented_out"]["lines"], [1])
        self.assertEqual(got["evidence"]["immune"], 0)

    def test_directive_gate_runs_before_the_short_inline_detector(self):
        """`is_directive` must run before the short-inline word count.

        `noqa` alone is one inline word; swapping the gates would flag it
        as short-inline noise instead of tool-directive metadata.
        """
        got = CM.classify_file("x = 1  # noqa\n", ".py")
        self.assertEqual(got["evidence"]["immune"], 1)
        self.assertEqual(got["evidence"]["short_inline"]["lines"], [])

    def test_coherence_gate_runs_before_the_short_inline_detector(self):
        """The coherence check must run before the short-inline word count.

        `# total` is one inline word but scores above the coherence bar
        against `compute_total`; swapping the gates would report it as
        short-inline instead of trivial-doc.
        """
        got = CM.classify_file(
            "def compute_total():\n    x = 5  # total\n", ".py")
        self.assertEqual(got["evidence"]["trivial_doc"]["lines"], [2])
        self.assertEqual(got["evidence"]["short_inline"]["lines"], [])


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

    def test_python_that_does_not_parse_is_counted_as_not_measured(self):
        # Newer syntax than the interpreter running the audit is the common
        # cause; `x = = 1` stands in for it because it fails the same stage.
        self._write("a.py", "x = 1\n")
        self._write("b.py", "# print(x)\nx = = 1\n")
        layer = CM.measure_comments(self.root, _Inventory(["a.py", "b.py"]), self.cfg)
        self.assertEqual([row["path"] for row in layer["files"]], ["a.py"])
        self.assertEqual(layer["not_measured_files"], 1)
        self.assertEqual(layer["status"], "partial")

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

    def test_a_brace_only_repository_is_partial_with_the_dependency_named(self):
        """`commented_out` is Python-only, so it never ran here.

        The per-file evidence is already `null` rather than an empty list;
        DESIGN.md's own rule -- "a metric computed from a layer that is not
        measured and coming out empty is reported as null and its own layer
        drops to partial with the dependency named" -- applies per metric,
        so the layer cannot announce `measured` for a half-measured one.
        """
        self._write("a.ts", "// x = 1;\nlet y = 2;\n")
        layer = CM.measure_comments(self.root, _Inventory(["a.ts"]), self.cfg)
        self.assertEqual(layer["status"], "partial")
        self.assertIn("commented_out", layer["reason"])
        self.assertEqual(layer["not_measured_files"], 0)
        self.assertIsNone(layer["files"][0]["evidence"]["commented_out"])

    def test_one_python_file_is_enough_to_make_the_layer_measured(self):
        self._write("a.ts", "let y = 2;\n")
        self._write("b.py", "x = 1\n")
        layer = CM.measure_comments(self.root, _Inventory(["a.ts", "b.py"]), self.cfg)
        self.assertEqual(layer["status"], "measured")
        self.assertIsNone(layer["reason"])

    def test_each_not_measured_cause_is_named_separately(self):
        # One reason string for all three told a reader that a `.rb` file, a
        # file the process cannot open, and a file using newer syntax were
        # the same problem.
        self._write("a.py", "x = 1\n")
        self._write("b.lua", "-- x\n")
        self._write("c.py", "x = = 1\n")
        layer = CM.measure_comments(
            self.root, _Inventory(["a.py", "b.lua", "c.py", "gone.py"]), self.cfg)
        self.assertEqual(layer["status"], "partial")
        self.assertEqual(layer["not_measured_files"], 3)
        self.assertIn("no comment reader for their extension", layer["reason"])
        self.assertIn("could not be read from disk", layer["reason"])
        self.assertIn("do not parse", layer["reason"])

    def test_languages_measured_is_reported(self):
        self._write("a.py", "x = 1\n")
        self._write("b.ts", "let x = 1;\n")
        layer = CM.measure_comments(self.root, _Inventory(["a.py", "b.ts"]), self.cfg)
        self.assertEqual(layer["languages_measured"], ["brace", "python"])

    def test_unreadable_file_is_skipped_without_raising(self):
        layer = CM.measure_comments(self.root, _Inventory(["missing.py"]), self.cfg)
        self.assertEqual(layer["files"], [])

    def test_empty_inventory_gives_not_measured_with_baseline_reason(self):
        """When inventory.source is empty, report the baseline reason.

        This is distinct from all files having no reader, all files being
        unreadable, or all Python files failing to parse -- those are three
        other causes already counted separately in the reasons list.
        """
        layer = CM.measure_comments(self.root, _Inventory([]), self.cfg)
        self.assertEqual(layer["status"], "not_measured")
        self.assertIn("no source file has a verified comment reader",
                      layer["reason"])
        self.assertEqual(layer["files"], [])


if __name__ == "__main__":
    unittest.main()

