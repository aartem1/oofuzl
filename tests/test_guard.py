import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import PLUGIN_ROOT

GUARD = PLUGIN_ROOT / "scripts" / "guard.py"


def run_guard(*args, cwd=None):
    return subprocess.run([sys.executable, str(GUARD)] + list(args),
                          capture_output=True, text=True, cwd=cwd)


def load_guard():
    """Import guard.py directly for unit tests of its internals (mirrors
    tests/test_parser_agreement.py's loader)."""
    spec = importlib.util.spec_from_file_location("vendored_guard_nesting", GUARD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_HAS_LIZARD = bool(load_guard().resolve_lizard())


class RatchetTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / ".code-health").mkdir()

    def baseline(self, files):
        path = self.root / ".code-health" / "baseline.json"
        path.write_text(json.dumps({"schema": 1, "head": "x",
                                    "thresholds": {}, "files": files}))
        return path

    def write(self, rel, lines):
        p = self.root / rel
        p.write_text("".join("x = %d\n" % i for i in range(lines)), encoding="utf-8")
        return rel

    def test_existing_file_that_shrinks_passes(self):
        self.baseline({"a.py": {"lines": 900, "tokens": 5000, "functions": {}}})
        self.write("a.py", 850)
        out = run_guard("a.py", cwd=self.root)
        self.assertEqual(out.returncode, 0, out.stdout)

    def test_existing_file_that_grows_beyond_its_baseline_fails(self):
        self.baseline({"a.py": {"lines": 900, "tokens": 5000, "functions": {}}})
        self.write("a.py", 950)
        out = run_guard("a.py", cwd=self.root)
        self.assertEqual(out.returncode, 1)
        self.assertIn("a.py", out.stdout)
        self.assertIn("900", out.stdout)

    def test_existing_small_file_may_grow_up_to_warn(self):
        # max(baseline 100, warn 400) == 400
        self.baseline({"a.py": {"lines": 100, "tokens": 500, "functions": {}}})
        self.write("a.py", 399)
        self.assertEqual(run_guard("a.py", cwd=self.root).returncode, 0)
        self.write("a.py", 400)
        self.assertEqual(run_guard("a.py", cwd=self.root).returncode, 1)

    def test_new_file_faces_warn_not_hard(self):
        # The whole point of the single rule: new code is not more privileged
        # than old code. A new 500-line file fails; an old 900-line one does not.
        self.baseline({})
        self.write("new.py", 500)
        out = run_guard("new.py", cwd=self.root)
        self.assertEqual(out.returncode, 1)
        self.assertIn("400", out.stdout)

    def test_new_file_below_warn_passes(self):
        self.baseline({})
        self.write("new.py", 399)
        self.assertEqual(run_guard("new.py", cwd=self.root).returncode, 0)

    def test_failure_names_both_escape_hatches(self):
        self.baseline({})
        self.write("new.py", 500)
        out = run_guard("new.py", cwd=self.root)
        self.assertIn("--no-verify", out.stdout)
        self.assertIn("update-baseline", out.stdout)

    def test_unsupported_extensions_are_skipped_silently(self):
        self.baseline({})
        (self.root / "README.md").write_text("# t\n" * 900, encoding="utf-8")
        self.assertEqual(run_guard("README.md", cwd=self.root).returncode, 0)

    def test_missing_baseline_passes_with_a_notice(self):
        (self.root / "a.py").write_text("x = 1\n", encoding="utf-8")
        out = run_guard("a.py", cwd=self.root)
        self.assertEqual(out.returncode, 0)
        self.assertIn("baseline", out.stdout.lower())

    def test_configured_excludes_are_skipped(self):
        self.baseline({})
        (self.root / ".code-health" / "config.json").write_text(
            json.dumps({"exclude": ["**/vendor/**"]}))
        (self.root / "vendor").mkdir()
        (self.root / "vendor" / "big.py").write_text("x = 1\n" * 900, encoding="utf-8")
        self.assertEqual(run_guard("vendor/big.py", cwd=self.root).returncode, 0)


class FunctionRatchetTests(RatchetTests):
    def test_existing_function_that_grows_fails(self):
        self.baseline({"a.py": {"lines": 50, "tokens": 400, "functions": {
            "f": {"lines": 120, "ccn": 5, "params": 2, "nesting": 1}}}})
        body = "def f(a, b):\n" + "    x = 1\n" * 130
        (self.root / "a.py").write_text(body, encoding="utf-8")
        out = run_guard("a.py", cwd=self.root)
        self.assertEqual(out.returncode, 1)
        self.assertIn("f", out.stdout)

    def test_new_function_over_warn_in_an_old_file_fails(self):
        self.baseline({"a.py": {"lines": 900, "tokens": 5000, "functions": {}}})
        body = "def brand_new(a, b):\n" + "    x = 1\n" * 70
        (self.root / "a.py").write_text(body, encoding="utf-8")
        out = run_guard("a.py", cwd=self.root)
        self.assertEqual(out.returncode, 1)
        self.assertIn("brand_new", out.stdout)

    def test_renamed_function_is_treated_as_new(self):
        self.baseline({"a.py": {"lines": 900, "tokens": 5000, "functions": {
            "old_name": {"lines": 200, "ccn": 5, "params": 2, "nesting": 1}}}})
        body = "def new_name(a, b):\n" + "    x = 1\n" * 70
        (self.root / "a.py").write_text(body, encoding="utf-8")
        out = run_guard("a.py", cwd=self.root)
        self.assertEqual(out.returncode, 1)


class BaselineTests(RatchetTests):
    def test_baseline_write_covers_files_and_functions(self):
        (self.root / "a.py").write_text("def f(a):\n    return a\n", encoding="utf-8")
        target = self.root / ".code-health" / "baseline.json"
        out = run_guard("--baseline", str(target), "--repo", str(self.root))
        self.assertEqual(out.returncode, 0, out.stderr)
        data = json.loads(target.read_text())
        self.assertIn("a.py", data["files"])
        self.assertIn("f", data["files"]["a.py"]["functions"])


class NestingTests(unittest.TestCase):
    """guard.py's own standalone AST nesting pass (it cannot import
    code_health). Reproduces, directly against guard's own code, the same
    defect verified against real lizard: a flat 5-`if` function must not
    report 5, and unmeasured languages must never fall back to lizard's
    broken number."""

    def setUp(self):
        self.guard = load_guard()

    def test_flat_python_function_is_depth_one(self):
        source = "def flat(x):\n" + "".join(
            "    if x == %d:\n        return %d\n" % (i, i) for i in range(5))
        by_line = self.guard._python_nesting(source)
        self.assertEqual(list(by_line.values()), [1])

    def test_deep_python_function_is_depth_five(self):
        source = (
            "def deep(x):\n"
            "    if x == 1:\n"
            "        if x == 2:\n"
            "            if x == 3:\n"
            "                if x == 4:\n"
            "                    if x == 5:\n"
            "                        return 5\n"
        )
        by_line = self.guard._python_nesting(source)
        self.assertEqual(list(by_line.values()), [5])

    def test_syntax_error_returns_empty_not_fatal(self):
        self.assertEqual(self.guard._python_nesting("def (:\n"), {})

    @unittest.skipUnless(_HAS_LIZARD, "lizard not installed")
    def test_measure_replaces_pythons_nesting(self):
        root = Path(tempfile.mkdtemp())
        (root / "flat.py").write_text(
            "def flat(x):\n" + "".join(
                "    if x == %d:\n        return %d\n" % (i, i) for i in range(5)),
            encoding="utf-8")
        files, had_lizard = self.guard.measure(root, ["flat.py"], 4)
        self.assertTrue(had_lizard)
        self.assertEqual(files["flat.py"]["functions"]["flat"]["nesting"], 1)

    @unittest.skipUnless(_HAS_LIZARD, "lizard not installed")
    def test_measure_reports_none_for_an_unverified_language(self):
        root = Path(tempfile.mkdtemp())
        (root / "flat.rb").write_text(
            "def flat(x)\n"
            "  return 1 if x == 1\n"
            "  return 0\n"
            "end\n",
            encoding="utf-8")
        files, had_lizard = self.guard.measure(root, ["flat.rb"], 4)
        self.assertTrue(had_lizard)
        self.assertIsNone(files["flat.rb"]["functions"]["flat"]["nesting"])

    def test_none_nesting_is_never_checked_and_never_crashes(self):
        guard = self.guard
        measured = {"a.rb": {"lines": 10, "tokens": 20, "functions": {
            "f": {"lines": 5, "ccn": 1, "params": 1, "nesting": None}}}}
        violations = guard.compare(measured, {"files": {}}, guard.DEFAULT_THRESHOLDS)
        self.assertEqual(violations, [])


class PerformanceTests(unittest.TestCase):
    def test_twenty_files_complete_within_the_budget(self):
        import time
        root = Path(tempfile.mkdtemp())
        (root / ".code-health").mkdir()
        (root / ".code-health" / "baseline.json").write_text(
            json.dumps({"schema": 1, "head": "x", "thresholds": {}, "files": {}}))
        names = []
        for index in range(20):
            name = "m%d.py" % index
            (root / name).write_text(
                "".join("def f%d():\n    return %d\n" % (i, i) for i in range(50)),
                encoding="utf-8")
            names.append(name)
        started = time.time()
        run_guard(*names, cwd=root)
        self.assertLess(time.time() - started, 5.0)


if __name__ == "__main__":
    unittest.main()
