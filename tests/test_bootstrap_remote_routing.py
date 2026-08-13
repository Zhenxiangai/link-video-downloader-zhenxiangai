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

    def test_official_account_backend_is_pinned_to_the_zhenxiangai_release(self):
        self.assertIn('release="v260810-zhenxiangai.2"', self.source)
        self.assertIn(
            "https://github.com/Zhenxiangai/wx_channels_download/releases/download/"
            "v260810-zhenxiangai.2/"
            "wx_video_download_v260810-zhenxiangai.2_darwin_arm64.zip",
            self.source,
        )
        self.assertIn(
            'release_sha256="e03b1bf8ec13d2412be8f10f6702bed6769dac75211be5023c71ea33ff871c4c"',
            self.source,
        )
        self.assertIn(
            'backend_sha256="fa9f56d119556ac4fff263871cd73add3217ec0b84950276ac645778f9020575"',
            self.source,
        )
        self.assertNotIn("github.com/ltaoo/wx_channels_download/releases/download", self.source)

    def test_explicit_recovery_window_wraps_probe_in_capture_cleanup(self):
        source = BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn("recover-channel-session) recover_channels_session", source)
        self.assertIn("capture_python recover-channel-session", source)
        self.assertIn("trap 'disable_capture", source)

    def test_recovery_budget_starts_before_capture_and_reserves_cleanup_time(self):
        body = self.function_body("recover_channels_session", "inspect_channel_author")
        self.assertIn('started_at=$(date +%s)', body)
        self.assertIn('--started-at "$started_at"', body)
        self.assertIn('--cleanup-reserve 30', body)

    def test_ephemeral_certificate_identity_is_recorded_before_generation(self):
        body = self.function_body("enable_capture", "restore_proxy")
        ephemeral = body.split('cert_name="wechat_archive_', 1)[1]
        record = 'echo "cert_name=$cert_name" >>"$proxy_snapshot"'
        generate = "api_post /api/proxy/certificate/generate"
        self.assertIn(record, ephemeral)
        self.assertLess(ephemeral.index(record), ephemeral.index(generate))

    def test_certificate_cleanup_does_not_ignore_keychain_query_failure(self):
        body = self.function_body("remove_capture_certificate", "unattended_ready")
        self.assertNotIn("2>/dev/null || true)", body)
        self.assertIn("security find-certificate -a -c \"$cert_name\"", body)
        self.assertIn("security find-certificate -a \"$keychain\"", body)
        self.assertIn("return 1", body)
        self.assertLess(body.index("return 1"), body.index('rm -f "$backend_runtime/certs/'))


if __name__ == "__main__":
    unittest.main()
