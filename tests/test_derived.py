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

    def imports(self, symbols=()):
        """A real imports layer always carries a status; `derived` now reads
        it, because a symbol table it never received is not an empty one."""
        return {"status": "measured", "symbols": list(symbols)}

    def churn(self):
        return {"status": "measured",
                "files": [{"path": "hot.py", "commits": 40, "authors": 5},
                          {"path": "cold.py", "commits": 1, "authors": 1}],
                "commit_sizes": [2, 2, 2, 10]}

    def test_churn_decides_the_hotspot_ranking(self):
        out = DV.measure_derived(self.size(), self.churn(), self.imports(), self.cfg)
        self.assertEqual(out["hotspots"][0]["path"], "hot.py")
        self.assertEqual(out["hotspots"][0]["score"], 40 * 20)

    def test_no_churn_makes_the_layer_not_measured(self):
        out = DV.measure_derived(self.size(),
                                 {"status": "not_measured", "reason": "no git"},
                                 self.imports(), self.cfg)
        self.assertEqual(out["status"], "not_measured")
        self.assertIn("churn", out["reason"])

    def test_symbol_collisions_need_the_configured_file_count(self):
        symbols = [{"name": "Report", "file": "a.py", "kind": "class", "tokens": 50},
                   {"name": "Report", "file": "b.py", "kind": "class", "tokens": 60},
                   {"name": "Report", "file": "c.py", "kind": "class", "tokens": 70},
                   {"name": "Once", "file": "a.py", "kind": "class", "tokens": 10}]
        out = DV.measure_derived(self.size(), self.churn(), self.imports(symbols), self.cfg)
        names = [row["name"] for row in out["symbol_collisions"]]
        self.assertEqual(names, ["Report"])
        self.assertEqual(out["symbol_collisions"][0]["files"], 3)

    def test_two_definitions_are_below_the_default_threshold(self):
        symbols = [{"name": "Report", "file": "a.py", "kind": "class", "tokens": 50},
                   {"name": "Report", "file": "b.py", "kind": "class", "tokens": 60}]
        out = DV.measure_derived(self.size(), self.churn(), self.imports(symbols), self.cfg)
        self.assertEqual(out["symbol_collisions"], [])

    def test_a_zero_median_falls_back_to_the_mean(self):
        # 3 of 4 commits touch one file, so the median other-files-per-commit
        # is 0. Dividing by it would zero every ratio while mean_co_changed is
        # non-zero -- a column of zeros that still looks like data.
        churn = dict(self.churn(), commit_sizes=[1, 1, 1, 5], files=[
            {"path": "hot.py", "commits": 1, "authors": 1, "mean_co_changed": 4.0},
            {"path": "cold.py", "commits": 1, "authors": 1, "mean_co_changed": 0.0},
        ])
        out = DV.measure_derived(self.size(), churn, self.imports(), self.cfg)
        self.assertEqual(out["edit_fanout_basis"], "mean")
        ratios = {r["path"]: r["ratio"] for r in out["edit_fanout"]}
        self.assertEqual(ratios["hot.py"], 4.0)   # mean of [0,0,0,4] is 1.0
        self.assertEqual(ratios["cold.py"], 0.0)

    def test_no_co_changes_at_all_reports_unavailable(self):
        churn = dict(self.churn(), commit_sizes=[1, 1, 1], files=[
            {"path": "solo.py", "commits": 3, "authors": 1, "mean_co_changed": 0.0},
        ])
        out = DV.measure_derived(self.size(), churn, self.imports(), self.cfg)
        self.assertEqual(out["edit_fanout_basis"], "unavailable")
        self.assertIsNone(out["edit_fanout"][0]["ratio"])

    def test_an_unmeasured_size_layer_never_reports_zero_hotspots(self):
        """With lizard absent, `size` is `partial` and hands over no
        function rows, so every hotspot score is zero and the list comes out
        empty. Reporting that as `measured` is a confident "this repository
        has no hotspots"."""
        out = DV.measure_derived({"status": "partial", "reason": "lizard failed"},
                                 self.churn(), self.imports(), self.cfg)
        self.assertEqual(out["status"], "partial")
        self.assertIsNone(out["hotspots"])
        self.assertIn("size", out["reason"])
        # edit_fanout depends only on churn, which IS measured -- it stays.
        self.assertIsNotNone(out["edit_fanout"])

    def test_an_unmeasured_imports_layer_never_reports_zero_collisions(self):
        out = DV.measure_derived(
            self.size(), self.churn(),
            {"status": "not_measured", "reason": "no Python sources", "symbols": []},
            self.cfg)
        self.assertEqual(out["status"], "partial")
        self.assertIsNone(out["symbol_collisions"])
        self.assertIn("imports", out["reason"])
        self.assertTrue(out["hotspots"])

    def test_edit_fanout_is_a_ratio_against_the_median_commit(self):
        # commit_sizes [2, 2, 2, 10] -> other-files-per-commit [1, 1, 1, 9], median 1.
        churn = dict(self.churn(), files=[
            {"path": "hot.py", "commits": 1, "authors": 1, "mean_co_changed": 9.0},
            {"path": "cold.py", "commits": 1, "authors": 1, "mean_co_changed": 1.0},
        ])
        out = DV.measure_derived(self.size(), churn, self.imports(), self.cfg)
        ratios = {row["path"]: row["ratio"] for row in out["edit_fanout"]}
        self.assertEqual(ratios["hot.py"], 9.0)
        self.assertEqual(ratios["cold.py"], 1.0)


if __name__ == "__main__":
    unittest.main()
