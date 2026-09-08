import tempfile
import unittest
from pathlib import Path

from helpers import load_package

load_package()
from code_health import config as C  # noqa: E402
from code_health import discovery as D  # noqa: E402
from code_health import sizes as S  # noqa: E402

# Verified real output of `lizard -ENS --csv`: 12 columns, no header,
# max_nested_structures last, long_name quoted because it contains commas.
SAMPLE_CSV = (
    '7,5,41,5,7,"outer@1-7@probe.py","probe.py","outer",'
    '"outer( a , b , c = 1 , * args , ** kw )",1,7,4\n'
    '2,1,7,1,2,"m@9-10@probe.py","probe.py","m","m( self )",9,10,0\n'
)


class CsvParseTests(unittest.TestCase):
    def test_parses_twelve_columns(self):
        rows = S.parse_lizard_csv(SAMPLE_CSV)
        self.assertEqual(len(rows), 2)
        first = rows[0]
        self.assertEqual(first["nloc"], 7)
        self.assertEqual(first["ccn"], 5)
        self.assertEqual(first["token"], 41)
        self.assertEqual(first["param"], 5)
        self.assertEqual(first["length"], 7)
        self.assertEqual(first["filename"], "probe.py")
        self.assertEqual(first["function_name"], "outer")
        self.assertEqual(first["start_line"], 1)
        self.assertEqual(first["end_line"], 7)
        self.assertEqual(first["nesting"], 4)

    def test_commas_inside_long_name_do_not_shift_columns(self):
        rows = S.parse_lizard_csv(SAMPLE_CSV)
        self.assertIn("* args", rows[0]["long_name"])
        self.assertEqual(rows[0]["nesting"], 4)

    def test_empty_output_is_not_an_error(self):
        # A function-free file produces no rows at all; this is why file-level
        # size must never come from lizard.
        self.assertEqual(S.parse_lizard_csv(""), [])

    def test_short_row_without_ens_is_rejected_loudly(self):
        short = '7,5,41,5,7,"outer@1-7@p.py","p.py","outer","outer( a )",1,7\n'
        with self.assertRaises(S.LizardOutputError):
            S.parse_lizard_csv(short)


class FileMeasureTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.cfg = C.load_config(self.root)

    def test_counts_lines_code_lines_and_tokens(self):
        (self.root / "a.py").write_text("x = 1\n\n# c\ny = 2\n", encoding="utf-8")
        rows = S.measure_files(self.root, ["a.py"], self.cfg)
        self.assertEqual(rows[0]["lines"], 4)
        self.assertEqual(rows[0]["code_lines"], 3)
        self.assertEqual(rows[0]["chars"], len("x = 1\n\n# c\ny = 2\n"))
        self.assertEqual(rows[0]["tokens"], C.estimate_tokens("x = 1\n\n# c\ny = 2\n", 4))

    def test_function_free_file_is_still_measured(self):
        body = "\n".join("CONST_%d = %d" % (i, i) for i in range(900)) + "\n"
        (self.root / "consts.py").write_text(body, encoding="utf-8")
        rows = S.measure_files(self.root, ["consts.py"], self.cfg)
        self.assertEqual(rows[0]["lines"], 900)


class LayerStatusTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        self.cfg = C.load_config(self.root)
        self.inv = D.enumerate_files(self.root, self.cfg)

    def test_missing_lizard_yields_partial_not_a_crash(self):
        original = S.resolve_lizard
        S.resolve_lizard = lambda: None
        try:
            layer = S.measure_sizes(self.root, self.inv, self.cfg)
        finally:
            S.resolve_lizard = original
        self.assertEqual(layer["status"], "partial")
        self.assertIn("lizard", layer["reason"])
        # File-level measurement does not need lizard, so it must still be there.
        self.assertEqual(len(layer["files"]), 1)
        self.assertEqual(layer["functions"], [])

    @unittest.skipUnless(S.resolve_lizard(), "lizard not installed")
    def test_with_lizard_functions_are_measured(self):
        layer = S.measure_sizes(self.root, self.inv, self.cfg)
        self.assertEqual(layer["status"], "measured")
        self.assertEqual(layer["functions"][0]["function_name"], "f")


class _Inv:
    """Minimal stand-in for discovery's inventory -- just what measure_sizes
    reads (`.source`, `.tests`)."""
    def __init__(self, source):
        self.source = source
        self.tests = []


@unittest.skipUnless(S.resolve_lizard(), "lizard not installed")
class NestingSourceTests(unittest.TestCase):
    """The actual defect, exercised end-to-end through measure_sizes, which
    is what a real audit run calls. Reproduced directly with real lizard
    before any of this was written:

        lizard -ENS --csv nest_flat.py            -> nesting=5, should be 1
        lizard -ENS --csv nest_flat.py x3 in argv  -> nesting=5, 10, 15
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.cfg = C.load_config(self.root)

    def test_python_nesting_is_replaced_not_lizards_accumulating_value(self):
        (self.root / "flat.py").write_text(
            "def flat(x):\n" + "".join(
                "    if x == %d:\n        return %d\n" % (i, i) for i in range(5)),
            encoding="utf-8")
        # The same file three times in one call is exactly the shape that
        # exposes lizard's cross-function accumulation (5, 10, 15 in argv
        # order); the fix must give the same, correct answer every time.
        layer = S.measure_sizes(self.root, _Inv(["flat.py", "flat.py", "flat.py"]), self.cfg)
        self.assertEqual([row["nesting"] for row in layer["functions"]], [1, 1, 1])
        self.assertEqual(layer["nesting_source"]["python_matched"], 3)
        self.assertEqual(layer["nesting_source"]["python_unmatched"], 0)

    def test_genuinely_deep_python_function_is_five(self):
        (self.root / "deep.py").write_text(
            "def deep(x):\n"
            "    if x == 1:\n"
            "        if x == 2:\n"
            "            if x == 3:\n"
            "                if x == 4:\n"
            "                    if x == 5:\n"
            "                        return 5\n",
            encoding="utf-8")
        layer = S.measure_sizes(self.root, _Inv(["deep.py"]), self.cfg)
        self.assertEqual(layer["functions"][0]["nesting"], 5)

    def test_brace_language_keeps_lizards_value(self):
        (self.root / "deep.js").write_text(
            "function deep(x) {\n"
            "    if (x === 1) {\n"
            "        if (x === 2) {\n"
            "            if (x === 3) {\n"
            "                return 3;\n"
            "            }\n"
            "        }\n"
            "    }\n"
            "    return 0;\n"
            "}\n",
            encoding="utf-8")
        layer = S.measure_sizes(self.root, _Inv(["deep.js"]), self.cfg)
        self.assertEqual(layer["functions"][0]["nesting"], 3)
        self.assertEqual(layer["nesting_source"]["kept_from_lizard"], 1)

    def test_unverified_language_reports_none_not_lizards_number(self):
        # Ruby's trailing-`if` modifier form was verified broken the same
        # way Python's is: `lizard -ENS --csv` reports 3 for a flat
        # 3-trailing-`if` function that is not nested at all.
        (self.root / "flat.rb").write_text(
            "def flat(x)\n"
            "  return 1 if x == 1\n"
            "  return 2 if x == 2\n"
            "  return 3 if x == 3\n"
            "  return 0\n"
            "end\n",
            encoding="utf-8")
        layer = S.measure_sizes(self.root, _Inv(["flat.rb"]), self.cfg)
        self.assertIsNone(layer["functions"][0]["nesting"])
        self.assertEqual(layer["nesting_source"]["not_measured"], 1)


if __name__ == "__main__":
    unittest.main()
