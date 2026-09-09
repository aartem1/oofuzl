import unittest

from helpers import load_package

load_package()
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
    IMMUNE = [
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
        "ms, not seconds",
        "end is exclusive",
        "returns None when the cache is cold",
        "the caller owns the handle and must close it",
        "workaround for a driver bug",
        "иначе счётчик сбрасывается на втором проходе",
        "-*- coding: utf-8 -*-",
        "!/usr/bin/env python3",
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

    def test_immune_texts(self):
        for text in self.IMMUNE:
            with self.subTest(text=text):
                self.assertTrue(CM.is_immune(text))

    def test_not_immune_texts(self):
        for text in self.NOT_IMMUNE:
            with self.subTest(text=text):
                self.assertFalse(CM.is_immune(text))


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

    def test_pathological_input_does_not_raise(self):
        # CPython's parser raises MemoryError on stack overflow from a long
        # unary-operator run. The detector must catch it, not crash the audit.
        pathological = "-" * 50000 + "1"
        result = CM.is_commented_out(pathological)
        self.assertFalse(result)


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


if __name__ == "__main__":
    unittest.main()
