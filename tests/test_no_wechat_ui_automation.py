import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "scripts" / "bootstrap.sh"
SKILL = ROOT / "SKILL.md"


class NoWechatUiAutomationTests(unittest.TestCase):
    def test_installer_does_not_enable_or_install_computer_use(self):
        source = BOOTSTRAP.read_text(encoding="utf-8")
        self.assertNotIn("install_computer_use", source)
        self.assertNotIn("hermes tools enable computer_use", source)
        self.assertNotIn("hermes computer-use install", source)
        self.assertNotIn("automatic task-local capture", source)

    def test_skill_never_instructs_hermes_to_control_wechat(self):
        source = SKILL.read_text(encoding="utf-8")
        self.assertNotIn("Hermes Computer Use", source)
        self.assertNotIn("Use Computer Use", source)
        self.assertNotIn("文件传输助手", source)


if __name__ == "__main__":
    unittest.main()
