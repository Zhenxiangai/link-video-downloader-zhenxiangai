import unittest
from pathlib import Path


BOOTSTRAP = Path(__file__).resolve().parents[1] / "scripts" / "bootstrap.sh"


class BootstrapRemoteRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = BOOTSTRAP.read_text(encoding="utf-8")

    def function_body(self, name: str, next_name: str) -> str:
        return self.source.split(f"{name}() {{", 1)[1].split(f"{next_name}() {{", 1)[0]

    def test_creator_inventory_does_not_enable_capture_by_default(self):
        body = self.function_body("inspect_creator", "download_creator_plan")
        self.assertNotIn("capture_python", body)
        self.assertIn("inspect-creator", body)

    def test_single_channels_link_does_not_enable_capture_by_default(self):
        body = self.function_body("download_channel_url", "status")
        self.assertNotIn("capture_python", body)
        self.assertIn("download-channel-url", body)

    def test_explicit_capture_commands_remain_available_for_manual_recovery(self):
        self.assertIn("enable-capture) enable_capture", self.source)
        self.assertIn("disable-capture) disable_capture", self.source)


if __name__ == "__main__":
    unittest.main()
