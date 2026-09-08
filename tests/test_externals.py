import tempfile
import unittest
from pathlib import Path

from helpers import load_package

load_package()
from code_health import config as C  # noqa: E402
from code_health import discovery as D  # noqa: E402
from code_health import externals as EX  # noqa: E402


class ProbeTests(unittest.TestCase):
    def test_absent_tool_reports_a_reason_and_does_not_raise(self):
        state = EX.probe("definitely-not-a-real-tool")
        self.assertFalse(state["available"])
        self.assertTrue(state["reason"])

class GracefulAbsenceTests(unittest.TestCase):
    ABSENT = {"available": False, "version": None,
              "reason": "not installed", "installed_by_us": False}

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "a.py").write_text("x = 1\n", encoding="utf-8")
        self.cfg = C.load_config(self.root)
        self.inv = D.enumerate_files(self.root, self.cfg)

    def test_missing_jscpd_yields_not_measured_with_a_reason(self):
        out = EX.run_jscpd(self.root, self.cfg, self.ABSENT)
        self.assertEqual(out["status"], "not_measured")
        self.assertIn("jscpd", out["reason"])

    def test_missing_madge_yields_not_measured_with_a_reason(self):
        out = EX.run_madge(self.root, ["a.js"], self.ABSENT)
        self.assertEqual(out["status"], "not_measured")
        self.assertIn("madge", out["reason"])

    def test_missing_dead_code_tools_yield_not_measured(self):
        out = EX.run_dead(self.root, self.inv,
                          {"vulture": self.ABSENT, "knip": self.ABSENT})
        self.assertEqual(out["status"], "not_measured")

    def test_survey_never_raises_and_covers_every_requested_tool(self):
        block = EX.survey(["lizard", "jscpd", "madge", "vulture", "knip"],
                          install_missing=False)
        self.assertEqual(sorted(block), ["jscpd", "knip", "lizard", "madge", "vulture"])
        for name, state in block.items():
            self.assertIn("available", state, name)
            if not state["available"]:
                self.assertTrue(state["reason"], name)


class MergeTests(unittest.TestCase):
    def test_madge_result_extends_the_python_layer(self):
        import measure  # scripts/ is on sys.path via helpers.load_package
        layer = {"status": "measured", "languages_measured": ["python"],
                 "cycles": [["pkg.a", "pkg.b"]], "degree": [], "reason": None}
        js = {"status": "measured", "graph": {"a.js": ["b.js"], "b.js": []},
              "cycles": []}
        out = measure._merge_js_imports(layer, js)
        self.assertEqual(out["languages_measured"], ["javascript", "python"])
        self.assertEqual(len(out["cycles"]), 1)
        self.assertEqual({d["module"] for d in out["degree"]}, {"a.js", "b.js"})

    def test_absent_madge_downgrades_a_measured_layer_to_partial(self):
        import measure
        layer = {"status": "measured", "languages_measured": ["python"],
                 "cycles": [], "degree": [], "reason": None}
        out = measure._merge_js_imports(
            layer, {"status": "not_measured", "reason": "madge unavailable: x",
                    "graph": {}, "cycles": []})
        self.assertEqual(out["status"], "partial")
        self.assertIn("madge", out["reason"])


if __name__ == "__main__":
    unittest.main()
