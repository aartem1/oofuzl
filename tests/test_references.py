import re
import unittest
from pathlib import Path

from helpers import PLUGIN_ROOT

REFERENCES = PLUGIN_ROOT / "references"
REQUIRED = ["metrics.md", "tools.md", "ledger.md", "sonar.md", "quality-policy.md"]


class ReferenceTests(unittest.TestCase):
    def test_every_skill_reference_link_resolves(self):
        pattern = re.compile(r"<plugin-root>/references/([\w.-]+)")
        for skill in (PLUGIN_ROOT / "skills").glob("*/SKILL.md"):
            for name in pattern.findall(skill.read_text(encoding="utf-8")):
                self.assertTrue((REFERENCES / name).is_file(),
                                "%s -> %s" % (skill, name))


if __name__ == "__main__":
    unittest.main()
