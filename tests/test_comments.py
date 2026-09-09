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


if __name__ == "__main__":
    unittest.main()
