import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "wechat_archive.py"
SPEC = importlib.util.spec_from_file_location("wechat_archive_waiting", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load wechat_archive module")
archive = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(archive)


class ContentWaitingMessageTests(unittest.TestCase):
    def test_channels_worker_waits_for_user_present_manual_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, job_dir, manifest = archive.new_job(root, "content", "https://weixin.qq.com/sph/one")
            manifest.update({"platform": "wechat_channels", "status": "queued"})
            archive.write_json(job_dir / "manifest.json", manifest)
            unavailable = archive.ArchiveError("channels_authorization_required", "offline", 69)
            with patch.object(archive, "process_channel_content", side_effect=unavailable):
                result = archive.process_content_job(job_dir / "manifest.json", root)

            self.assertEqual(result["status"], "waiting_for_authorization")
            message = result["next_action"]
            self.assertIn("用户在 Mac 微信中手动打开", message)
            self.assertNotIn("自动启用", message)
            self.assertNotIn("自动打开", message)
            self.assertNotIn("微信聊天", message)
            saved = json.loads((job_dir / "manifest.json").read_text())
            self.assertEqual(saved["next_action"], message)


if __name__ == "__main__":
    unittest.main()
