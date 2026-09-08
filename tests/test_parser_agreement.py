import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import PLUGIN_ROOT, load_package

load_package()
from code_health import pyast as PA  # noqa: E402
from code_health import sizes as S  # noqa: E402

# Every numeric field holds a DISTINCT value on purpose. With coincidental
# duplicates (the real lizard row this is modelled on has ccn == param == 5 and
# nloc == length == end_line == 7) a column swap between two equal fields is
# invisible, and catching column swaps is this file's entire reason to exist.
# Consistency check: length == end_line - start_line + 1 == 9.
SAMPLE_CSV = (
    '7,5,41,3,9,"outer@11-19@probe.py","probe.py","outer",'
    '"outer( a , b , c = 1 , * args , ** kw )",11,19,4\n'
)


def load_guard():
    spec = importlib.util.spec_from_file_location(
        "vendored_guard", PLUGIN_ROOT / "scripts" / "guard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ParserAgreementTests(unittest.TestCase):
    """guard.py duplicates the CSV parse on purpose -- it runs where
    code_health does not exist. This keeps the two honest."""

    def test_both_parsers_produce_identical_numbers(self):
        guard = load_guard()
        mine = S.parse_lizard_csv(SAMPLE_CSV)[0]
        theirs = guard.parse_lizard_csv(SAMPLE_CSV)[0]
        for field in ("nloc", "ccn", "token", "param", "length",
                      "start_line", "end_line", "nesting"):
            self.assertEqual(mine[field], theirs[field], field)
        self.assertEqual(mine["function_name"], theirs["function_name"])
        # Guard the guard: if the fixture ever regains duplicate numeric values,
        # a swap between the duplicated columns stops being detectable.
        numbers = [mine[f] for f in ("nloc", "ccn", "token", "param", "length",
                                     "start_line", "end_line", "nesting")]
        self.assertEqual(len(numbers), len(set(numbers)),
                         "fixture must keep every numeric field distinct")

    def test_both_use_the_same_source_extension_set(self):
        """The audit and the ratchet must agree on what a file even is.

        They did not: `discovery.enumerate_files` measured every tracked
        non-binary file, so 40 `.md` files were measured as source on this
        repository while `guard.py` had always filtered by `SUPPORTED_EXT`.
        """
        from code_health import discovery as D
        guard = load_guard()
        self.assertEqual(set(D.SOURCE_EXT), set(guard.SUPPORTED_EXT))

    def test_both_use_the_same_default_thresholds(self):
        from code_health import config as C
        guard = load_guard()
        for key, value in C.DEFAULTS["thresholds"].items():
            if key in guard.DEFAULT_THRESHOLDS:
                self.assertEqual(guard.DEFAULT_THRESHOLDS[key], value, key)

    def test_both_agree_on_threshold_comparison(self):
        from code_health import config as C
        guard = load_guard()
        threshold = {"warn": 400, "hard": 800}
        for value in (399, 400, 799, 800, 801):
            self.assertEqual(
                C.level_for(value, threshold) is not None,
                guard.exceeds(value, 400),
                value)


class NestingAgreementTests(unittest.TestCase):
    """guard.py duplicates the nesting-depth AST pass for the same reason it
    duplicates the CSV parser: it cannot import code_health. This is the
    corresponding cross-check for that duplication."""

    def test_guard_and_pyast_agree_on_a_flat_python_fixture(self):
        source = "def flat(x):\n" + "".join(
            "    if x == %d:\n        return %d\n" % (i, i) for i in range(5))
        root = Path(tempfile.mkdtemp())
        (root / "flat.py").write_text(source, encoding="utf-8")

        guard = load_guard()
        theirs = guard._python_nesting(source)
        mine, errors = PA.measure_nesting(root, ["flat.py"])

        self.assertEqual(errors, [])
        self.assertEqual(len(mine), 1)
        self.assertEqual(list(theirs.values()), [mine[0]["nesting"]])
        # Pin the actual number too: an agreement test that both sides get
        # wrong the same way would still pass without this.
        self.assertEqual(mine[0]["nesting"], 1)

    def test_guard_and_pyast_agree_on_a_genuinely_nested_python_fixture(self):
        source = (
            "def deep(x):\n"
            "    if x == 1:\n"
            "        if x == 2:\n"
            "            if x == 3:\n"
            "                return 3\n"
        )
        root = Path(tempfile.mkdtemp())
        (root / "deep.py").write_text(source, encoding="utf-8")

        guard = load_guard()
        theirs = guard._python_nesting(source)
        mine, errors = PA.measure_nesting(root, ["deep.py"])

        self.assertEqual(errors, [])
        self.assertEqual(list(theirs.values()), [mine[0]["nesting"]])
        self.assertEqual(mine[0]["nesting"], 3)

    def test_guard_and_pyast_agree_on_non_if_constructs(self):
        """Both fixtures above reach their depth entirely through `if`
        chains. Proved by mutation that this leaves a real gap: breaking
        guard.py's `for`/`while`/`try` branch of `_max_depth` (the
        `depth + 1` in the non-`If` arm of the loop) left both of them
        green, because neither one ever exercises that branch. This
        fixture reaches its depth only through
        `for` -> `while` -> `try`/`except` -> `with` (and, when the running
        interpreter supports it, one more level through `match`/`case`),
        with the expected depth pinned as its own assertion -- so this
        test fails on either a disagreement between the two
        implementations OR an absolute value both get wrong the same way.
        """
        base = (
            "def f(xs):\n"
            "    for x in xs:\n"
            "        while x:\n"
            "            try:\n"
            "                with open('a') as fh:\n"
        )
        if sys.version_info >= (3, 10):
            base += (
                "                    match x:\n"
                "                        case 1:\n"
                "                            return 1\n"
                "                        case _:\n"
                "                            return 0\n"
            )
            expected = 5     # for -> while -> try -> with -> match/case
        else:
            base += "                    return 1\n"
            expected = 4     # for -> while -> try -> with
        source = base + (
            "            except ValueError:\n"
            "                pass\n"
            "    return 0\n"
        )

        root = Path(tempfile.mkdtemp())
        (root / "mixed.py").write_text(source, encoding="utf-8")

        guard = load_guard()
        theirs = guard._python_nesting(source)
        mine, errors = PA.measure_nesting(root, ["mixed.py"])

        self.assertEqual(errors, [])
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["nesting"], expected)
        self.assertEqual(list(theirs.values()), [mine[0]["nesting"]])


if __name__ == "__main__":
    unittest.main()
