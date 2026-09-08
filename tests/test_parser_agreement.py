import importlib.util
import unittest

from helpers import PLUGIN_ROOT, load_package

load_package()
from code_health import sizes as S  # noqa: E402

SAMPLE_CSV = (
    '7,5,41,5,7,"outer@1-7@probe.py","probe.py","outer",'
    '"outer( a , b , c = 1 , * args , ** kw )",1,7,4\n'
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


if __name__ == "__main__":
    unittest.main()
