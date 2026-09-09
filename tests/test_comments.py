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


if __name__ == "__main__":
    unittest.main()
