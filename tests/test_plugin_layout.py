import json
import shutil
import subprocess
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
# This repository ships one plugin at its root, so the plugin root and the
# repository root are the same directory. In `personal_ai_tools`, where this
# plugin used to live under `plugins/code-health/`, they were two levels apart.
REPO_ROOT = PLUGIN_ROOT
NAME = "code-health"


def plugin_manifest(rel):
    return json.loads((PLUGIN_ROOT / rel).read_text())


def marketplace_entry(rel, name):
    """The entry for `name` in a marketplace manifest, or None.

    By membership, never by position: these tests used to assert the whole
    plugin-name list and index into `plugins[1]`, so adding any unrelated
    plugin to this repository broke them for a reason that has nothing to do
    with code-health.
    """
    data = json.loads((REPO_ROOT / rel).read_text())
    for entry in data["plugins"]:
        if entry["name"] == name:
            return entry
    return None


class PluginLayoutTests(unittest.TestCase):
    def test_both_manifests_agree_on_name_and_version(self):
        claude = plugin_manifest(".claude-plugin/plugin.json")
        codex = plugin_manifest(".codex-plugin/plugin.json")
        self.assertEqual(claude["name"], NAME)
        self.assertEqual(codex["name"], NAME)
        # Version is compared BETWEEN the two manifests rather than against a
        # literal typed here: a literal only ever asserts that the same
        # release bump was typed twice in one commit.
        self.assertEqual(claude["version"], codex["version"])

    def test_the_claude_marketplace_points_at_this_plugin_at_this_version(self):
        entry = marketplace_entry(".claude-plugin/marketplace.json", NAME)
        self.assertIsNotNone(entry, "code-health is not listed in the Claude marketplace")
        self.assertEqual(entry["source"], "./")
        self.assertEqual(entry["version"],
                         plugin_manifest(".claude-plugin/plugin.json")["version"])

    def test_the_codex_marketplace_points_at_this_plugin(self):
        entry = marketplace_entry(".agents/plugins/marketplace.json", NAME)
        self.assertIsNotNone(entry, "code-health is not listed in the Codex marketplace")
        self.assertEqual(entry["source"]["path"], "./")

    @unittest.skipUnless(shutil.which("claude"), "the claude CLI is not installed")
    def test_claude_plugin_validate_passes(self):
        # --strict turns warnings (missing skill description, unrecognized
        # fields) into exit 1. This is why there is no hand-rolled frontmatter
        # test: the real validator covers more and stays correct as it evolves.
        out = subprocess.run(
            ["claude", "plugin", "validate", ".", "--strict"],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)


if __name__ == "__main__":
    unittest.main()
