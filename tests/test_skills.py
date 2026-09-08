import unittest

from helpers import PLUGIN_ROOT

SKILLS = PLUGIN_ROOT / "skills"


class SkillTests(unittest.TestCase):
    def test_three_skills_exist(self):
        self.assertEqual(sorted(p.parent.name for p in SKILLS.glob("*/SKILL.md")),
                         ["audit", "fix", "guard"])

    def test_each_skill_has_a_codex_agent_file(self):
        for path in SKILLS.glob("*/SKILL.md"):
            self.assertTrue((path.parent / "agents" / "openai.yaml").is_file(), path)

if __name__ == "__main__":
    unittest.main()
