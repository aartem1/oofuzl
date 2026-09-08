import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import PLUGIN_ROOT, load_package

load_package()
from code_health import config as C  # noqa: E402
from code_health import findings as F  # noqa: E402


class LayerResolutionTests(unittest.TestCase):
    def test_derived_pulls_in_size_churn_and_imports(self):
        # `imports` is there because `measure_derived` reads its symbol
        # table; without the declaration `--layers derived` produced no
        # collisions at all, and `fix` runs exactly that before deciding a
        # group's findings are gone.
        self.assertEqual(F.resolve_layers(["derived"]),
                         {"derived", "size", "churn", "imports"})

    def test_every_documented_layer_dependency_is_declared(self):
        """LAYER_DEPS is what `--layers` resolves transitively, so a
        dependency missing from it is a layer silently measured without its
        input."""
        for name in F.LAYER_NAMES:
            for dep in F.LAYER_DEPS.get(name, ()):
                self.assertIn(dep, F.LAYER_NAMES, "%s -> %s" % (name, dep))

    def test_only_the_tools_the_resolved_layers_can_use_are_surveyed(self):
        """`survey` probed all five tools regardless of `--layers`:
        `--layers size --files <one file>` took 3.6 s, ~3.4 s of it
        `npx --no-install` startup for tools that layer cannot consume, and
        `fix` runs a narrow measure twice per queue item."""
        self.assertEqual(F.tools_for(F.resolve_layers(["size"])), ["lizard"])
        self.assertEqual(F.tools_for(F.resolve_layers(["churn"])), [])
        self.assertEqual(F.tools_for(F.resolve_layers(["dup"])), ["jscpd", "lizard"])
        self.assertEqual(F.tools_for(F.resolve_layers(F.LAYER_NAMES)),
                         ["jscpd", "lizard", "madge", "vulture"])

    def test_dup_pulls_in_size(self):
        self.assertEqual(F.resolve_layers(["dup"]), {"dup", "size"})

    def test_unknown_layer_is_rejected(self):
        with self.assertRaises(ValueError):
            F.resolve_layers(["sizes"])


class SizeFindingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = C.load_config(Path(tempfile.mkdtemp()))

    def test_file_at_hard_limit_fires_hard(self):
        layer = {"files": [{"path": "a.py", "lines": 800, "tokens": 10, "chars": 10}],
                 "functions": []}
        out = F.size_findings(layer, self.cfg)
        hit = [f for f in out if f["kind"] == "file_too_long"]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["level"], "hard")
        self.assertEqual(hit[0]["id"], "file_too_long:a.py")

    def test_file_one_below_warn_is_clean(self):
        layer = {"files": [{"path": "a.py", "lines": 399, "tokens": 10, "chars": 10}],
                 "functions": []}
        self.assertEqual(F.size_findings(layer, self.cfg), [])

    def test_eight_parameters_fires_hard(self):
        layer = {"files": [], "functions": [{
            "filename": "a.py", "function_name": "f", "param": 8, "ccn": 1,
            "length": 3, "nesting": 0, "start_line": 1, "end_line": 3, "token": 10,
        }]}
        hit = [f for f in F.size_findings(layer, self.cfg) if f["kind"] == "too_many_params"]
        self.assertEqual(hit[0]["level"], "hard")
        self.assertEqual(hit[0]["value"], 8)

    def test_none_nesting_produces_no_finding_and_does_not_crash(self):
        # sizes.py sets nesting=None for a language it never verified
        # lizard's nesting reader for (or an unmatched Python row); this
        # must never be silently treated as zero, and must never reach the
        # >= comparison that would crash on None.
        layer = {"files": [], "functions": [{
            "filename": "a.rb", "function_name": "f", "param": 1, "ccn": 1,
            "length": 3, "nesting": None, "start_line": 1, "end_line": 3, "token": 10,
        }]}
        out = F.size_findings(layer, self.cfg)
        self.assertFalse(any(f["kind"] == "too_deep_nesting" for f in out))

class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.cfg = C.load_config(Path(tempfile.mkdtemp()))

    def _layer(self, line_counts):
        return {"files": [{"path": "f%d.py" % i, "lines": n, "tokens": n * 10, "chars": n * 40}
                          for i, n in enumerate(line_counts)],
                "test_files": [], "functions": []}

    def test_by_cut_reports_named_cuts(self):
        class Inv:
            source = ["a"] * 4
            tests = []
            skipped = {"binary": 0, "too_large": 0, "symlink": 0}
        summary = F.build_summary(self._layer([1200, 900, 500, 100]), Inv, self.cfg)
        self.assertEqual(sorted(summary["by_cut"]), ["1000", "400", "800"])
        self.assertEqual(summary["by_cut"]["1000"]["files"], 1)
        self.assertEqual(summary["by_cut"]["800"]["files"], 2)
        self.assertEqual(summary["by_cut"]["400"]["files"], 3)
        self.assertEqual(summary["loc"], 2700)
        # One decimal, matching the spec's own example (`"loc_pct": 42.5`) and
        # the figure the report quotes. Anyone needing full precision has both
        # `loc` and the cut's `loc` in the same document.
        self.assertEqual(summary["by_cut"]["1000"]["loc_pct"], 44.4)


class CliTests(unittest.TestCase):
    def test_measure_py_emits_valid_json_and_exits_zero(self):
        repo = Path(tempfile.mkdtemp())
        (repo / "a.py").write_text("def f(a, b):\n    return a + b\n", encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(PLUGIN_ROOT / "scripts" / "measure.py"),
             "--repo", str(repo)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = json.loads(proc.stdout)
        self.assertEqual(doc["schema"], 1)
        self.assertEqual(sorted(doc["layers"]), sorted(F.LAYER_NAMES))
        for name, layer in doc["layers"].items():
            self.assertIn(layer["status"], ("measured", "partial", "not_measured"), name)
            if layer["status"] != "measured":
                self.assertTrue(layer.get("reason"), name)

    def test_nonexistent_repo_is_a_usage_error(self):
        # Exit 0 with an all-zero document would tell a user their repository
        # is empty. "Valid document produced" is the exit-0 contract, and no
        # document can be produced for a path that is not there.
        proc = subprocess.run(
            [sys.executable, str(PLUGIN_ROOT / "scripts" / "measure.py"),
             "--repo", "/nonexistent/path/xyz"],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout.strip(), "")
        self.assertIn("not a directory", proc.stderr)

    def test_exit_is_zero_even_with_findings(self):
        repo = Path(tempfile.mkdtemp())
        (repo / "big.py").write_text("x = 1\n" * 900, encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(PLUGIN_ROOT / "scripts" / "measure.py"),
             "--repo", str(repo)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0)
        doc = json.loads(proc.stdout)
        self.assertTrue(any(f["kind"] == "file_too_long" for f in doc["findings"]))


class CloneAndDeadFindingTests(unittest.TestCase):
    """`exact_clone` and `dead_symbol` are documented finding kinds that used
    to be emitted nowhere -- jscpd's clones stopped at `layers.dup.exact` and
    vulture's symbols at `layers.dead.symbols`."""

    def cfg(self):
        return C.load_config(Path(tempfile.mkdtemp()))

    def clone(self, first, second):
        # Field names and shape taken from a real `jscpd@5 --reporters json`
        # report produced while writing this. Each side is (path, start line).
        return {"format": "javascript", "lines": 24, "tokens": 375,
                "firstFile": {"name": first[0], "start": first[1],
                              "end": first[1] + 23},
                "secondFile": {"name": second[0], "start": second[1],
                               "end": second[1] + 23}}

    def test_a_clone_id_does_not_depend_on_which_side_jscpd_named_first(self):
        cfg = self.cfg()
        left, right = ("src/a.js", 1), ("src/b.js", 7)
        forward = F.clone_findings([self.clone(left, right)], cfg)
        backward = F.clone_findings([self.clone(right, left)], cfg)
        self.assertEqual(len(forward), 1)
        self.assertEqual(forward[0]["id"], backward[0]["id"])
        self.assertEqual(forward[0]["kind"], "exact_clone")

    def test_a_dead_symbol_id_survives_a_line_shift(self):
        early = F.dead_findings({"symbols": [
            {"file": "a.py", "line": "12", "name": "compute",
             "confidence": 60, "message": "unused function 'compute'"}]})
        later = F.dead_findings({"symbols": [
            {"file": "a.py", "line": "48", "name": "compute",
             "confidence": 60, "message": "unused function 'compute'"}]})
        self.assertEqual(early[0]["id"], later[0]["id"])
        self.assertEqual(early[0]["kind"], "dead_symbol")
        # The tool's own confidence travels with the finding: both dead-code
        # tools have known false positives on dynamic dispatch.
        self.assertEqual(early[0]["value"], 60)


if __name__ == "__main__":
    unittest.main()
