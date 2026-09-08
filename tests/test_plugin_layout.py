import json
import subprocess
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_ROOT.parents[1]


class PluginLayoutTests(unittest.TestCase):
    def test_both_manifests_name_the_plugin_code_health(self):
        for rel in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json"):
            data = json.loads((PLUGIN_ROOT / rel).read_text())
            self.assertEqual(data["name"], "code-health", rel)
            self.assertEqual(data["version"], "0.3.0", rel)

    def test_codex_interface_stays_russian(self):
        data = json.loads((PLUGIN_ROOT / ".codex-plugin/plugin.json").read_text())
        text = data["interface"]["shortDescription"]
        self.assertTrue(any("Ѐ" <= ch <= "ӿ" for ch in text))

    def test_claude_marketplace_renamed_in_place(self):
        data = json.loads((REPO_ROOT / ".claude-plugin/marketplace.json").read_text())
        names = [p["name"] for p in data["plugins"]]
        self.assertEqual(names[0], "code-health")
        self.assertEqual(names, ["code-health", "sp", "mp"])
        entry = data["plugins"][0]
        self.assertEqual(entry["source"], "./plugins/code-health")
        self.assertEqual(entry["version"], "0.3.0")

    def test_codex_marketplace_renamed_in_place(self):
        data = json.loads((REPO_ROOT / ".agents/plugins/marketplace.json").read_text())
        names = [p["name"] for p in data["plugins"]]
        self.assertEqual(
            names,
            ["palmier-cinematic-youtube", "code-health", "sp", "openwrt-router-operations"],
        )
        entry = data["plugins"][1]
        self.assertEqual(entry["source"]["path"], "./plugins/code-health")

    def test_cursor_marketplace_untouched(self):
        data = json.loads((REPO_ROOT / ".cursor-plugin/marketplace.json").read_text())
        self.assertEqual([p["name"] for p in data["plugins"]], ["sp"])

    def test_claude_plugin_validate_passes(self):
        # --strict turns warnings (missing skill description, unrecognized
        # fields) into exit 1. This is why there is no hand-rolled frontmatter
        # test: the real validator covers more and stays correct as it evolves.
        out = subprocess.run(
            ["claude", "plugin", "validate", "plugins/code-health", "--strict"],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)


if __name__ == "__main__":
    unittest.main()
