import json
import tempfile
import unittest
from pathlib import Path

from helpers import load_package

load_package()
from code_health import config as C  # noqa: E402


class ThresholdSemanticsTests(unittest.TestCase):
    T = {"warn": 400, "hard": 800}

    def test_value_equal_to_warn_fires_warn(self):
        self.assertEqual(C.level_for(400, self.T), "warn")

    def test_value_one_below_warn_is_clean(self):
        self.assertIsNone(C.level_for(399, self.T))

    def test_value_equal_to_hard_fires_hard(self):
        self.assertEqual(C.level_for(800, self.T), "hard")

    def test_value_between_fires_warn(self):
        self.assertEqual(C.level_for(799, self.T), "warn")

    def test_parameters_hard_is_eight_and_eight_fires(self):
        # The established figure is "114 functions with >= 8 parameters".
        self.assertEqual(C.DEFAULTS["thresholds"]["parameters"]["hard"], 8)
        self.assertEqual(C.level_for(8, C.DEFAULTS["thresholds"]["parameters"]), "hard")


class DefaultsTests(unittest.TestCase):
    def test_every_documented_threshold_is_present(self):
        expected = {
            "file_lines": (400, 800),
            "file_tokens": (8000, 15000),
            "function_lines": (60, 100),
            "function_ccn": (15, 25),
            "function_cognitive": (15, 25),
            "parameters": (5, 8),
            "nesting": (3, 4),
        }
        for key, (warn, hard) in expected.items():
            t = C.DEFAULTS["thresholds"][key]
            self.assertEqual((t["warn"], t["hard"]), (warn, hard), key)
        self.assertEqual(C.DEFAULTS["chars_per_token"], 4)

class LoadConfigTests(unittest.TestCase):
    def _repo(self, config_obj=None):
        d = Path(tempfile.mkdtemp())
        if config_obj is not None:
            (d / ".code-health").mkdir()
            (d / ".code-health" / "config.json").write_text(json.dumps(config_obj))
        return d

    def test_absent_config_yields_defaults(self):
        cfg = C.load_config(self._repo())
        self.assertEqual(cfg["thresholds"]["file_lines"]["hard"], 800)

    def test_partial_override_keeps_other_defaults(self):
        cfg = C.load_config(self._repo({"thresholds": {"file_lines": {"hard": 500}}}))
        self.assertEqual(cfg["thresholds"]["file_lines"]["hard"], 500)
        self.assertEqual(cfg["thresholds"]["file_lines"]["warn"], 400)
        self.assertEqual(cfg["thresholds"]["parameters"]["hard"], 8)

    def test_unknown_top_level_key_is_rejected(self):
        with self.assertRaises(C.ConfigError):
            C.load_config(self._repo({"treshholds": {}}))

    def test_unknown_threshold_key_is_rejected(self):
        with self.assertRaises(C.ConfigError):
            C.load_config(self._repo({"thresholds": {"file_length": {"warn": 1}}}))

    def test_warn_above_hard_is_rejected(self):
        with self.assertRaises(C.ConfigError):
            C.load_config(self._repo({"thresholds": {"file_lines": {"warn": 900}}}))


class TokenEstimateTests(unittest.TestCase):
    def test_exact_multiple(self):
        self.assertEqual(C.estimate_tokens("x" * 400, 4), 100)

    def test_rounds_up(self):
        self.assertEqual(C.estimate_tokens("x" * 401, 4), 101)

    def test_counts_characters_not_bytes(self):
        # 4 Cyrillic characters are 8 UTF-8 bytes; the estimate must say 1, not 2.
        self.assertEqual(C.estimate_tokens("кэша", 4), 1)


if __name__ == "__main__":
    unittest.main()
