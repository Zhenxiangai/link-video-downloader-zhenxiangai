import importlib.util
import json
import fcntl
import os
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
    def test_content_job_lock_prevents_duplicate_processing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, job_dir, manifest = archive.new_job(root, "content", "https://weixin.qq.com/sph/one")
            manifest.update({"platform": "wechat_channels", "status": "queued"})
            manifest_path = job_dir / "manifest.json"
            archive.write_json(manifest_path, manifest)
            lock_path = manifest_path.with_suffix(".json.lock")
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch.object(archive, "process_channel_content") as process:
                    result = archive.process_content_job(manifest_path, root)
                process.assert_not_called()
                self.assertEqual(result["status"], "queued")
            finally:
                os.close(descriptor)

    def test_manual_content_resume_does_not_requeue_when_content_lock_is_busy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job_id, job_dir, manifest = archive.new_job(root, "content", "https://weixin.qq.com/sph/one")
            manifest.update({"platform": "wechat_channels", "status": "waiting_for_authorization", "channel_object": {"id": "one"}})
            manifest_path = job_dir / "manifest.json"
            archive.write_json(manifest_path, manifest)
            lock_path = manifest_path.with_suffix(".json.lock")
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch.object(archive, "process_channel_content") as process:
                    result = archive.resume_job(job_id, root)
                process.assert_not_called()
            finally:
                os.close(descriptor)
            saved = json.loads(manifest_path.read_text())
            self.assertEqual(saved["status"], "waiting_for_authorization")
            self.assertEqual(result["status"], "waiting_for_authorization")

    def test_frozen_channel_object_survives_transient_submit_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, job_dir, manifest = archive.new_job(root, "content", "https://weixin.qq.com/sph/one")
            frozen = {"id": "object-1", "objectDesc": {"mediaType": 4, "media": [{"url": "https://example.invalid/video"}]}}
            manifest.update(
                {
                    "platform": "wechat_channels",
                    "status": "downloading",
                    "title": "frozen",
                    "content_id": "object-1",
                    "channel_object": frozen,
                }
            )
            archive.write_json(job_dir / "manifest.json", manifest)
            unavailable = archive.ArchiveError("channels_authorization_required", "offline", 69)

            with patch.object(archive, "submit_channel_objects", side_effect=unavailable):
                result = archive.process_content_job(job_dir / "manifest.json", root)

            saved = json.loads((job_dir / "manifest.json").read_text())
            self.assertEqual(result["status"], "waiting_for_authorization")
            self.assertEqual(saved["channel_object"], frozen)

    def test_recovery_deadline_reaches_channel_submit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, job_dir, manifest = archive.new_job(root, "content", "https://weixin.qq.com/sph/one")
            manifest.update({"platform": "wechat_channels", "status": "waiting_for_authorization", "channel_object": {"id": "one"}})
            archive.write_json(job_dir / "manifest.json", manifest)
            with patch.object(archive, "process_channel_content", return_value={"status": "downloading"}) as process:
                archive.process_content_job(job_dir / "manifest.json", root, resume_waiting=True, deadline=130.0)
            self.assertEqual(process.call_args.kwargs["deadline"], 130.0)

    def test_recovery_deadline_reaches_share_eid_resolution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, job_dir, manifest = archive.new_job(root, "content", "https://weixin.qq.com/sph/one")
            manifest.update({"platform": "wechat_channels", "status": "downloading"})
            archive.write_json(job_dir / "manifest.json", manifest)
            unavailable = archive.ArchiveError("channels_authorization_required", "offline", 69)
            with patch.object(archive, "resolve_channel_share_eid", side_effect=unavailable) as resolve:
                archive.process_content_job(job_dir / "manifest.json", root, deadline=130.0, start_only=True)
            self.assertEqual(resolve.call_args.kwargs["deadline"], 130.0)

    def test_recovery_deadline_expiry_keeps_frozen_job_waiting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, job_dir, manifest = archive.new_job(root, "content", "https://weixin.qq.com/sph/one")
            frozen = {
                "id": "one",
                "objectDesc": {
                    "description": "one",
                    "mediaType": 4,
                    "media": [{"url": "https://example.invalid/video"}],
                },
            }
            manifest.update({"platform": "wechat_channels", "status": "downloading", "channel_object": frozen})
            archive.write_json(job_dir / "manifest.json", manifest)
            expired = archive.ArchiveError("recovery_window_expired", "expired", 69)
            with patch.object(archive, "submit_channel_objects", side_effect=expired):
                result = archive.process_content_job(job_dir / "manifest.json", root, deadline=130.0, start_only=True)
            self.assertEqual(result["status"], "waiting_for_authorization")
            self.assertEqual(result["channel_object"], frozen)
            self.assertNotIn("completed_at", result)

    def test_recovery_window_does_not_poll_existing_download_or_start_transcription(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, job_dir, manifest = archive.new_job(root, "content", "https://weixin.qq.com/sph/one")
            manifest.update(
                {
                    "platform": "wechat_channels",
                    "status": "waiting_for_authorization",
                    "upstream_task_ids": [42],
                }
            )
            archive.write_json(job_dir / "manifest.json", manifest)
            with patch.object(archive, "process_channel_content") as process:
                result = archive.process_content_job(
                    job_dir / "manifest.json",
                    root,
                    resume_waiting=True,
                    deadline=130.0,
                    start_only=True,
                )
            process.assert_not_called()
            self.assertEqual(result["status"], "downloading")

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

    def test_channels_backend_transient_waits_even_outside_explicit_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, job_dir, manifest = archive.new_job(root, "content", "https://weixin.qq.com/sph/one")
            manifest.update(
                {
                    "platform": "wechat_channels",
                    "status": "downloading",
                    "channel_object": {"id": "one", "objectDesc": {"description": "one"}},
                }
            )
            archive.write_json(job_dir / "manifest.json", manifest)
            unavailable = archive.ArchiveError("channels_backend_error", "temporary", 69)
            with patch.object(archive, "process_channel_content", side_effect=unavailable):
                result = archive.process_content_job(job_dir / "manifest.json", root)
            self.assertEqual(result["status"], "waiting_for_authorization")
            self.assertNotIn("completed_at", result)


if __name__ == "__main__":
    unittest.main()
