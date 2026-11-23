"""Latest-stable resolution of the measuring tools (`--update-tools`).

The network and every subprocess are fakes: what is under test is the
decision logic -- what is asked of the registry, which install command is
issued, what the `tools` block and the history line record.
"""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from helpers import load_package

load_package()
import measure  # noqa: E402
from code_health import config as C  # noqa: E402
from code_health import discovery as D  # noqa: E402
from code_health import externals as EX  # noqa: E402


class _Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class FakeWorld:
    """Registry, PATH and installers, patched into `externals`."""

    def __init__(self, registry=None, installed=None, on_path=("uv", "npx"),
                 install_works=True):
        self.registry = registry or {}        # url -> version | Exception
        self.installed = dict(installed or {})  # python tool -> version on PATH
        self.on_path = set(on_path)
        self.install_works = install_works
        self.urls, self.commands = [], []

    def http_json(self, url, timeout=None):
        self.urls.append(url)
        value = self.registry.get(url, OSError("no route to registry"))
        if isinstance(value, Exception):
            raise value
        return value

    def which(self, name):
        if name in self.installed or name in self.on_path:
            return "/bin/" + name
        return None

    def run(self, argv, timeout=120, cwd=None):
        self.commands.append(list(argv))
        tool = argv[0].rsplit("/", 1)[-1]
        if tool in self.installed and argv[1:] == ["--version"]:
            bare = tool in ("jscpd", "madge", "knip")      # node tools print the number only
            return _Proc(0, ("%s\n" if bare else tool + " %s\n") % self.installed[tool])
        if argv[:3] == ["uv", "tool", "install"]:
            if not self.install_works:
                return _Proc(1, "", "boom")
            spec = argv[-1]
            name, _, version = spec.partition("==")
            self.installed[name] = version or "9.9.9"
            self.on_path.add(name)
            return _Proc(0)
        if argv[:2] == ["npx", "-y"] and argv[-1] == "--version":
            if not self.install_works:
                return _Proc(1, "", "boom")
            return _Proc(0, "npm notice\n%s\n" % argv[2].rpartition("@")[2])
        return _Proc(1, "", "unexpected")

    def installs(self):
        return [c for c in self.commands if c[:3] == ["uv", "tool", "install"]
                or c[:2] == ["npx", "-y"]]

    def __enter__(self):
        self._saved = (EX._http_json, EX._run, EX.shutil.which, EX._venv_bin)
        EX._http_json, EX._run = self.http_json, self.run
        EX.shutil.which, EX._venv_bin = self.which, lambda name: None
        return self

    def __exit__(self, *exc):
        EX._http_json, EX._run, EX.shutil.which, EX._venv_bin = self._saved


PYPI_LIZARD = "https://pypi.org/pypi/lizard/json"
NPM_JSCPD = "https://registry.npmjs.org/jscpd/latest"


class LatestVersionTests(unittest.TestCase):
    def test_python_tool_reads_pypi_info_version(self):
        with FakeWorld(registry={PYPI_LIZARD: {"info": {"version": "1.18.0"}}}) as w:
            self.assertEqual(EX.latest_version("lizard"), ("1.18.0", None))
        self.assertEqual(w.urls, [PYPI_LIZARD])

    def test_npx_tool_reads_the_npm_latest_tag(self):
        with FakeWorld(registry={NPM_JSCPD: {"version": "5.9.0"}}) as w:
            self.assertEqual(EX.latest_version("jscpd"), ("5.9.0", None))
        self.assertEqual(w.urls, [NPM_JSCPD])

    def test_unreachable_registry_gives_a_one_line_reason(self):
        with FakeWorld(registry={PYPI_LIZARD: OSError("timed out")}):
            version, error = EX.latest_version("lizard")
        self.assertIsNone(version)
        self.assertIn("timed out", error)
        self.assertNotIn("\n", error)

    def test_a_prerelease_is_not_taken(self):
        with FakeWorld(registry={PYPI_LIZARD: {"info": {"version": "2.0.0rc1"}}}):
            version, error = EX.latest_version("lizard")
        self.assertIsNone(version)
        self.assertIn("stable", error)

    def test_an_unexpected_document_is_an_error_not_a_crash(self):
        with FakeWorld(registry={PYPI_LIZARD: {"oops": 1}}):
            version, error = EX.latest_version("lizard")
        self.assertIsNone(version)
        self.assertTrue(error)

    def test_unknown_tool(self):
        version, error = EX.latest_version("nope")
        self.assertIsNone(version)
        self.assertTrue(error)


class UpdateSurveyTests(unittest.TestCase):
    def test_older_install_is_upgraded_to_exactly_the_latest(self):
        world = FakeWorld(registry={PYPI_LIZARD: {"info": {"version": "1.18.0"}}},
                          installed={"lizard": "1.17.10"})
        with world:
            state = EX.survey(["lizard"], update=True)["lizard"]
        self.assertIn(["uv", "tool", "install", "--force", "lizard==1.18.0"],
                      world.commands)
        self.assertEqual(state["version"], "1.18.0")
        self.assertEqual(state["latest"], "1.18.0")
        self.assertTrue(state["updated"])
        self.assertEqual(state["previous_version"], "1.17.10")
        self.assertIsNone(state["update_error"])
        self.assertTrue(state["available"])

    def test_current_install_is_left_alone(self):
        world = FakeWorld(registry={PYPI_LIZARD: {"info": {"version": "1.18.0"}}},
                          installed={"lizard": "1.18.0"})
        with world:
            state = EX.survey(["lizard"], update=True)["lizard"]
        self.assertEqual(world.installs(), [])
        self.assertFalse(state["updated"])
        self.assertEqual(state["latest"], "1.18.0")
        self.assertEqual(state["version"], "1.18.0")
        self.assertEqual(state["previous_version"], "1.18.0")

    def test_unreachable_registry_keeps_the_installed_tool(self):
        world = FakeWorld(installed={"lizard": "1.17.10"})   # no registry routes
        with world:
            state = EX.survey(["lizard"], update=True)["lizard"]
        self.assertEqual(world.installs(), [])
        self.assertTrue(state["available"])
        self.assertEqual(state["version"], "1.17.10")
        self.assertIsNone(state["latest"])
        self.assertFalse(state["updated"])
        self.assertIn("registry", state["update_error"])

    def test_unreachable_registry_installs_the_pinned_fallback_when_missing(self):
        world = FakeWorld()
        with world:
            state = EX.survey(["lizard"], update=True)["lizard"]
        self.assertTrue(state["available"], state.get("reason"))
        self.assertEqual(state["version"], EX.LIZARD_VERSION)
        self.assertTrue(state["installed_by_us"])
        self.assertTrue(state["update_error"])

    def test_failed_upgrade_keeps_the_old_tool_and_says_why(self):
        world = FakeWorld(registry={PYPI_LIZARD: {"info": {"version": "1.18.0"}}},
                          installed={"lizard": "1.17.10"}, install_works=False)
        with world:
            state = EX.survey(["lizard"], update=True)["lizard"]
        self.assertTrue(state["available"])
        self.assertEqual(state["version"], "1.17.10")
        self.assertFalse(state["updated"])
        self.assertEqual(state["latest"], "1.18.0")
        self.assertIn("1.18.0", state["update_error"])

    def test_a_shadowing_binary_is_reported_not_hidden(self):
        # The install succeeded, but PATH still resolves another lizard.
        world = FakeWorld(registry={PYPI_LIZARD: {"info": {"version": "1.18.0"}}},
                          installed={"lizard": "1.17.10"})
        original = world.run

        def run(argv, timeout=120, cwd=None):
            proc = original(argv, timeout, cwd)
            if argv[:3] == ["uv", "tool", "install"]:
                world.installed["lizard"] = "1.17.10"   # shadowed
            return proc

        world.run = run
        with world:
            state = EX.survey(["lizard"], update=True)["lizard"]
        self.assertEqual(state["version"], "1.17.10")
        self.assertFalse(state["updated"])
        self.assertIn("1.18.0", state["update_error"])

    def test_npx_tool_runs_through_the_resolved_version(self):
        world = FakeWorld(registry={NPM_JSCPD: {"version": "5.9.0"}})
        with world:
            state = EX.survey(["jscpd"], update=True)["jscpd"]
            self.assertIn(["npx", "-y", "jscpd@5.9.0", "--version"], world.commands)
            self.assertEqual(state["version"], "5.9.0")
            self.assertEqual(state["latest"], "5.9.0")
            self.assertEqual(EX._npx_argv("jscpd", state)[:3],
                             ["npx", "-y", "jscpd@5.9.0"])

    def test_npx_run_uses_the_resolved_spec_in_the_real_invocation(self):
        world = FakeWorld(registry={NPM_JSCPD: {"version": "5.9.0"}})
        with world:
            state = EX.survey(["jscpd"], update=True)["jscpd"]
            world.commands.clear()
            root = Path(tempfile.mkdtemp())
            cfg = C.load_config(root)
            EX.run_jscpd(root, cfg, state)
        self.assertEqual(world.commands[0][:3], ["npx", "-y", "jscpd@5.9.0"])

    def test_npx_path_binary_that_is_old_is_replaced_by_the_latest_through_npx(self):
        world = FakeWorld(registry={NPM_JSCPD: {"version": "5.9.0"}},
                          installed={"jscpd": "5.4.1"})
        with world:
            state = EX.survey(["jscpd"], update=True)["jscpd"]
        self.assertEqual(state["previous_version"], "5.4.1")
        self.assertEqual(state["version"], "5.9.0")
        self.assertTrue(state["updated"])
        self.assertNotIn("bin", state)

    def test_npx_registry_unreachable_falls_back_to_the_pinned_spec(self):
        world = FakeWorld()
        with world:
            state = EX.survey(["jscpd"], update=True)["jscpd"]
            self.assertIn(["npx", "-y", EX.TOOLS["jscpd"]["package"], "--version"],
                          world.commands)
        self.assertTrue(state["available"], state.get("reason"))
        self.assertTrue(state["update_error"])
        self.assertIsNone(state["latest"])

    def test_without_update_the_registry_is_not_touched(self):
        world = FakeWorld(installed={"lizard": "1.17.10"})
        with world:
            state = EX.survey(["lizard"])["lizard"]
        self.assertEqual(world.urls, [])
        self.assertEqual(world.installs(), [])
        for key in ("latest", "previous_version", "update_error"):
            self.assertIsNone(state[key], key)
        self.assertIs(state["updated"], False)
        self.assertEqual(state["version"], "1.17.10")

    def test_an_absent_tool_carries_the_new_keys_too(self):
        with FakeWorld():
            state = EX.survey(["lizard"])["lizard"]
        self.assertFalse(state["available"])
        for key in ("latest", "updated", "previous_version", "update_error"):
            self.assertIn(key, state)


class ProvisionSpecTests(unittest.TestCase):
    def test_python_install_without_a_version_uses_the_pin(self):
        self.assertEqual(EX.install_commands("lizard")[0],
                         ["uv", "tool", "install", "lizard==%s" % EX.LIZARD_VERSION])

    def test_python_install_with_a_version_forces_it_through_every_channel(self):
        cmds = EX.install_commands("lizard", "1.18.0")
        self.assertEqual(cmds[0], ["uv", "tool", "install", "--force", "lizard==1.18.0"])
        self.assertEqual(cmds[1], ["pipx", "install", "--force", "lizard==1.18.0"])

    def test_npx_spec_uses_at(self):
        self.assertEqual(EX.install_commands("madge", "9.0.0")[0],
                         ["npx", "-y", "madge@9.0.0", "--version"])

    def test_the_plugin_venv_gets_the_requested_version(self):
        world = FakeWorld(on_path=())     # neither uv nor pipx
        calls = []
        world.run = lambda argv, timeout=120, cwd=None: calls.append(list(argv)) or _Proc(0)
        with world:
            EX.provision("lizard", "1.18.0")
        self.assertEqual(calls[-1], [str(EX._VENV / "bin" / "pip"), "install",
                                     "lizard==1.18.0"])


class ToolChangeTests(unittest.TestCase):
    def test_a_changed_version_is_reported(self):
        prev = {"tools": {"lizard": "1.17.10", "vulture": "2.16"}}
        doc = {"tools": {"lizard": {"available": True, "version": "1.18.0"},
                         "vulture": {"available": True, "version": "2.16"}}}
        self.assertEqual(measure.tool_changes(prev, doc),
                         [{"tool": "lizard", "before": "1.17.10", "after": "1.18.0"}])

    def test_unchanged_unavailable_and_unknown_before_are_ignored(self):
        prev = {"tools": {"lizard": "1.0", "madge": "8.0.0"}}
        doc = {"tools": {"lizard": {"available": True, "version": "1.0"},
                         "madge": {"available": False, "version": None},
                         "vulture": {"available": True, "version": "2.16"}}}
        self.assertEqual(measure.tool_changes(prev, doc), [])

    def test_no_previous_line(self):
        doc = {"tools": {"lizard": {"available": True, "version": "1.0"}}}
        self.assertEqual(measure.tool_changes(None, doc), [])
        self.assertEqual(measure.tool_changes({}, doc), [])

    def test_note_text(self):
        note = measure.tool_changes_note([
            {"tool": "lizard", "before": "1.23.0", "after": "1.24.0"},
            {"tool": "vulture", "before": "2.16", "after": "2.17"}])
        self.assertEqual(note, "Инструменты обновились с прошлого аудита: "
                               "lizard 1.23.0 → 1.24.0, vulture 2.16 → 2.17")
        self.assertIsNone(measure.tool_changes_note([]))

    def test_history_line_records_versions_of_available_tools(self):
        doc = {"schema": 1, "generated_at": "t", "repo": {"head": "h"}, "summary": {},
               "layers": {}, "findings": [],
               "tools": {"lizard": {"available": True, "version": "1.18.0"},
                         "madge": {"available": False, "version": None}}}
        self.assertEqual(measure.history_line(doc)["tools"], {"lizard": "1.18.0"})

    def test_last_history_line_skips_blank_and_broken_tails(self):
        path = Path(tempfile.mkdtemp()) / "h.jsonl"
        self.assertIsNone(measure.last_history_line(str(path)))
        path.write_text('{"a": 1}\n{"a": 2}\n\n', encoding="utf-8")
        self.assertEqual(measure.last_history_line(str(path)), {"a": 2})
        path.write_text('{"a": 1}\nnot json\n', encoding="utf-8")
        self.assertIsNone(measure.last_history_line(str(path)))


class MainWiringTests(unittest.TestCase):
    def _repo(self):
        root = Path(tempfile.mkdtemp())
        (root / "a.py").write_text("x = 1\n", encoding="utf-8")
        return root

    def _run_main(self, argv, tools_block):
        calls = []
        original = EX.survey

        def survey(names, install_missing=False, update=False):
            calls.append({"names": list(names), "install_missing": install_missing,
                          "update": update})
            return {n: dict(tools_block[n]) for n in names if n in tools_block}

        EX.survey = survey
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                code = measure.main(argv)
        finally:
            EX.survey = original
        return code, out.getvalue(), calls

    def test_update_tools_implies_install_missing_and_asks_for_the_update(self):
        root = self._repo()
        code, _, calls = self._run_main(
            ["--repo", str(root), "--layers", "size", "--update-tools"], {})
        self.assertEqual(code, 0)
        self.assertEqual(calls[0]["names"], ["lizard"])
        self.assertTrue(calls[0]["install_missing"])
        self.assertTrue(calls[0]["update"])

    def test_default_run_does_not_update(self):
        root = self._repo()
        _, _, calls = self._run_main(["--repo", str(root), "--layers", "size"], {})
        self.assertFalse(calls[0]["update"])
        self.assertFalse(calls[0]["install_missing"])

    def test_brief_names_the_tool_upgrade_since_the_last_audit(self):
        root = self._repo()
        history = Path(tempfile.mkdtemp()) / "h.jsonl"
        history.write_text(json.dumps({"tools": {"lizard": "1.23.0"}}) + "\n",
                           encoding="utf-8")
        block = {"lizard": {"available": False, "version": "1.24.0", "reason": "x"}}
        block["lizard"]["available"] = True
        argv = ["--repo", str(root), "--layers", "size", "--brief", "3",
                "--history", str(history)]
        _, text, _ = self._run_main(argv, block)
        brief = json.loads(text)
        self.assertIn("Инструменты обновились с прошлого аудита: lizard 1.23.0 → 1.24.0",
                      brief["tool_changes_note"])
        self.assertEqual(brief["tool_changes"],
                         [{"tool": "lizard", "before": "1.23.0", "after": "1.24.0"}])
        lines = history.read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(lines[-1])["tools"], {"lizard": "1.24.0"})
        # second run: nothing changed, nothing to say
        _, text, _ = self._run_main(argv, block)
        brief = json.loads(text)
        self.assertNotIn("tool_changes_note", brief)
        self.assertNotIn("tool_changes", brief)


class VultureVersionRobustnessTests(unittest.TestCase):
    """A new vulture major may change exit codes or output. The layer must
    degrade with a reason, never report a clean zero."""

    def _dead(self, proc):
        root = Path(tempfile.mkdtemp())
        (root / "m.py").write_text("import os\n", encoding="utf-8")
        inv = D.enumerate_files(root, C.load_config(root))
        original = EX._run
        EX._run = lambda argv, timeout=None, cwd=None: proc
        try:
            return EX.run_dead(root, inv, {"vulture": {"available": True, "bin": "vulture"}})
        finally:
            EX._run = original

    def test_dead_code_found_with_exit_3_is_measured(self):
        out = self._dead(_Proc(3, "m.py:1: unused import 'os' (90% confidence)\n"))
        self.assertEqual(out["status"], "measured")
        self.assertEqual([s["name"] for s in out["symbols"]], ["os"])

    def test_an_unknown_exit_code_is_not_measured(self):
        out = self._dead(_Proc(4, "", "new behaviour"))
        self.assertEqual(out["status"], "not_measured")
        self.assertIn("exit 4", out["reason"])

    def test_output_the_parser_does_not_understand_is_not_a_clean_zero(self):
        out = self._dead(_Proc(0, "Found 1 unused thing in m.py\n"))
        self.assertNotEqual(out["status"], "measured")
        self.assertIn("parse", out["reason"])


if __name__ == "__main__":
    unittest.main()
