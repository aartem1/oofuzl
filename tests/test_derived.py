import tempfile
import unittest
from pathlib import Path

from helpers import load_package

load_package()
from code_health import config as C  # noqa: E402
from code_health import derived as DV  # noqa: E402


class DerivedTests(unittest.TestCase):
    def setUp(self):
        self.cfg = C.load_config(Path(tempfile.mkdtemp()))

    def size(self):
        return {"status": "measured",
                "files": [{"path": "hot.py", "lines": 900, "tokens": 9000},
                          {"path": "cold.py", "lines": 900, "tokens": 9000}],
                "functions": [
                    {"filename": "hot.py", "function_name": "f", "ccn": 20,
                     "start_line": 1, "end_line": 9, "token": 100},
                    {"filename": "cold.py", "function_name": "g", "ccn": 20,
                     "start_line": 1, "end_line": 9, "token": 100},
                ]}

    def churn(self):
        return {"status": "measured",
                "files": [{"path": "hot.py", "commits": 40, "authors": 5},
                          {"path": "cold.py", "commits": 1, "authors": 1}],
                "commit_sizes": [2, 2, 2, 10]}

    def test_churn_decides_the_hotspot_ranking(self):
        out = DV.measure_derived(self.size(), self.churn(), {"symbols": []}, self.cfg)
        self.assertEqual(out["hotspots"][0]["path"], "hot.py")
        self.assertEqual(out["hotspots"][0]["score"], 40 * 20)

    def test_no_churn_makes_the_layer_not_measured(self):
        out = DV.measure_derived(self.size(),
                                 {"status": "not_measured", "reason": "no git"},
                                 {"symbols": []}, self.cfg)
        self.assertEqual(out["status"], "not_measured")
        self.assertIn("churn", out["reason"])

    def test_symbol_collisions_need_the_configured_file_count(self):
        symbols = [{"name": "Report", "file": "a.py", "kind": "class", "tokens": 50},
                   {"name": "Report", "file": "b.py", "kind": "class", "tokens": 60},
                   {"name": "Report", "file": "c.py", "kind": "class", "tokens": 70},
                   {"name": "Once", "file": "a.py", "kind": "class", "tokens": 10}]
        out = DV.measure_derived(self.size(), self.churn(), {"symbols": symbols}, self.cfg)
        names = [row["name"] for row in out["symbol_collisions"]]
        self.assertEqual(names, ["Report"])
        self.assertEqual(out["symbol_collisions"][0]["files"], 3)

    def test_two_definitions_are_below_the_default_threshold(self):
        symbols = [{"name": "Report", "file": "a.py", "kind": "class", "tokens": 50},
                   {"name": "Report", "file": "b.py", "kind": "class", "tokens": 60}]
        out = DV.measure_derived(self.size(), self.churn(), {"symbols": symbols}, self.cfg)
        self.assertEqual(out["symbol_collisions"], [])

    def test_edit_fanout_is_a_ratio_against_the_median_commit(self):
        # commit_sizes [2, 2, 2, 10] -> other-files-per-commit [1, 1, 1, 9], median 1.
        churn = dict(self.churn(), files=[
            {"path": "hot.py", "commits": 1, "authors": 1, "mean_co_changed": 9.0},
            {"path": "cold.py", "commits": 1, "authors": 1, "mean_co_changed": 1.0},
        ])
        out = DV.measure_derived(self.size(), churn, {"symbols": []}, self.cfg)
        ratios = {row["path"]: row["ratio"] for row in out["edit_fanout"]}
        self.assertEqual(ratios["hot.py"], 9.0)
        self.assertEqual(ratios["cold.py"], 1.0)


if __name__ == "__main__":
    unittest.main()
