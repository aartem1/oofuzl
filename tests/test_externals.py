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

    def test_madge_is_given_the_file_list_not_the_repository_root(self):
        """`relpaths` was read for the emptiness check and then ignored, so
        `measure.py --files a.js` still walked the whole repository."""
        seen = []

        def fake_run(argv, timeout=None, cwd=None):
            seen.append(argv)
            return type("P", (), {"returncode": 0, "stdout": "{}"})()

        original = EX._run
        EX._run = fake_run
        try:
            EX.run_madge(self.root, ["a.js", "b.ts", "c.py"],
                         {"available": True, "reason": None})
        finally:
            EX._run = original
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][-2:], ["a.js", "b.ts"])
        self.assertNotIn(str(self.root), seen[0])

    def test_survey_never_raises_and_covers_every_requested_tool(self):
        block = EX.survey(["lizard", "jscpd", "madge", "vulture", "knip"],
                          install_missing=False)
        self.assertEqual(sorted(block), ["jscpd", "knip", "lizard", "madge", "vulture"])
        for name, state in block.items():
            self.assertIn("available", state, name)
            if not state["available"]:
                self.assertTrue(state["reason"], name)

    def test_a_vulture_that_cannot_parse_a_file_does_not_claim_measured(self):
        # vulture exits 1 for both "found dead code" and "error"; only stderr
        # separates them. Reporting `measured` with zero symbols after a failure
        # is a confident "nothing found" that is indistinguishable from a real
        # clean run.
        root = Path(tempfile.mkdtemp())
        (root / "bad.py").write_text("def (:\n", encoding="utf-8")
        cfg = C.load_config(root)
        inv = D.enumerate_files(root, cfg)
        state = EX.probe("vulture")
        if not state["available"]:
            self.skipTest("vulture not installed")
        out = EX.run_dead(root, inv, {"vulture": state, "knip": self.ABSENT})
        self.assertEqual(out["status"], "partial")
        self.assertIn("vulture", out["reason"])


class NpxProvisioningTests(unittest.TestCase):
    """`npx -y <pkg>` populates npx's own cache, which `npx --no-install` never
    consults: the tool that was just installed successfully was then reported
    as unavailable, and jscpd/madge layers came back `partial` on a machine
    where both tools work."""

    def _fake_run(self, script):
        calls = []

        def fake(argv, timeout=None, cwd=None):
            calls.append(argv)
            rc, out = script(argv)
            return type("P", (), {"returncode": rc, "stdout": out, "stderr": ""})()

        return calls, fake

    def test_provision_trusts_the_install_command_it_just_ran(self):
        def script(argv):
            if argv[:2] == ["npx", "-y"]:
                return 0, "jscpd 5.2.1\n"
            return 1, ""            # every --no-install probe misses the cache

        calls, fake = self._fake_run(script)
        original, original_which = EX._run, EX.shutil.which
        # npx itself is on PATH; the tool it installs is not.
        EX._run = fake
        EX.shutil.which = lambda name: "/usr/local/bin/npx" if name == "npx" else None
        try:
            state = EX.provision("jscpd")
        finally:
            EX._run, EX.shutil.which = original, original_which
        self.assertTrue(state["available"], state.get("reason"))
        self.assertTrue(state["installed_by_us"])
        self.assertIn("5.2.1", state["version"])

    def test_probe_uses_a_tool_already_on_path(self):
        def script(argv):
            if argv[0] == "/usr/local/bin/madge":
                return 0, "8.0.0\n"
            return 1, ""

        calls, fake = self._fake_run(script)
        original, original_which = EX._run, EX.shutil.which
        EX._run = fake
        EX.shutil.which = lambda name: "/usr/local/bin/madge" if name == "madge" else None
        try:
            state = EX.probe("madge")
        finally:
            EX._run, EX.shutil.which = original, original_which
        self.assertTrue(state["available"], state.get("reason"))
        self.assertEqual(state["version"], "8.0.0")
        self.assertEqual(state["bin"], "/usr/local/bin/madge")
        self.assertNotIn("npx", [a[0] for a in calls])

    def test_absent_npx_tool_names_both_lookups_in_its_reason(self):
        original, original_which = EX._run, EX.shutil.which
        EX._run = lambda argv, timeout=None, cwd=None: type(
            "P", (), {"returncode": 1, "stdout": "", "stderr": ""})()
        EX.shutil.which = lambda name: None
        try:
            state = EX.probe("jscpd")
        finally:
            EX._run, EX.shutil.which = original, original_which
        self.assertFalse(state["available"])
        self.assertIn("PATH", state["reason"])
        self.assertIn("npx", state["reason"])

    def test_runners_invoke_the_path_binary_when_probe_found_one(self):
        calls, fake = self._fake_run(lambda argv: (0, "{}"))
        original = EX._run
        EX._run = fake
        try:
            EX.run_madge(Path(tempfile.mkdtemp()), ["a.js"],
                         {"available": True, "reason": None,
                          "bin": "/usr/local/bin/madge"})
        finally:
            EX._run = original
        self.assertEqual(calls[0][0], "/usr/local/bin/madge")
        self.assertNotIn("npx", calls[0])


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
