import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "wechat_archive.py"
SPEC = importlib.util.spec_from_file_location("wechat_archive_worker", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load wechat_archive module")
archive = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(archive)


class ChannelsWaitingJobWorkerTests(unittest.TestCase):
    def create_waiting_job(self, root: Path) -> tuple[str, Path, dict]:
        job_id, job_dir, manifest = archive.new_job(root, "batch", "https://weixin.qq.com/sph/new")
        manifest.update(
            {
                "kind": "creator_batch",
                "platform": "wechat_channels",
                "status": "waiting_for_authorization",
                "session_retry_after": "2000-01-01T00:00:00Z",
            }
        )
        archive.write_json(job_dir / "manifest.json", manifest)
        return job_id, job_dir, manifest

    def test_worker_resumes_one_waiting_creator_when_session_returns(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job_id, job_dir, _ = self.create_waiting_job(root)
            with patch.object(
                archive,
                "inspect_channel_creator",
                return_value={"ok": True, "job_id": job_id, "status": "awaiting_download_count"},
            ) as inspect:
                resumed = archive.resume_waiting_channel_creators_once(root)

            self.assertEqual(resumed, 1)
            inspect.assert_called_once()
            args, kwargs = inspect.call_args
            self.assertEqual(args[0], "https://weixin.qq.com/sph/new")
            self.assertEqual(kwargs["existing_job"][0], job_id)

    def test_worker_throttles_session_retries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, job_dir, manifest = self.create_waiting_job(root)
            manifest["session_retry_after"] = (datetime.now(timezone.utc) + timedelta(minutes=10)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            archive.write_json(job_dir / "manifest.json", manifest)
            with patch.object(archive, "inspect_channel_creator") as inspect:
                resumed = archive.resume_waiting_channel_creators_once(root)
            self.assertEqual(resumed, 0)
            inspect.assert_not_called()

    def test_selected_batch_requeues_existing_children_without_reinventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, job_dir, manifest = self.create_waiting_job(root)
            child_id, child_dir, child = archive.new_job(root, "content", "https://weixin.qq.com/sph/child")
            child.update({"platform": "wechat_channels", "status": "waiting_for_authorization"})
            archive.write_json(child_dir / "manifest.json", child)
            manifest.update(
                {
                    "selection": {"limit": 1, "order": "newest"},
                    "child_job_ids": [child_id],
                    "inventory": {"items": [{"child_job_id": child_id}]},
                }
            )
            archive.write_json(job_dir / "manifest.json", manifest)

            with patch.object(archive, "inspect_channel_creator") as inspect:
                resumed = archive.resume_waiting_channel_creators_once(root)

            self.assertEqual(resumed, 1)
            inspect.assert_not_called()
            saved = json.loads((job_dir / "manifest.json").read_text())
            self.assertEqual(saved["selection"]["limit"], 1)
            self.assertEqual(saved["child_job_ids"], [child_id])
            resumed_child = json.loads((child_dir / "manifest.json").read_text())
            self.assertEqual(resumed_child["status"], "queued")

    def test_failed_retry_stays_waiting_and_sets_next_low_frequency_probe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, job_dir, _ = self.create_waiting_job(root)
            waiting = {"ok": True, "status": "waiting_for_authorization"}
            with patch.object(archive, "inspect_channel_creator", return_value=waiting):
                resumed = archive.resume_waiting_channel_creators_once(root, retry_interval=900)
            self.assertEqual(resumed, 0)
            saved = json.loads((job_dir / "manifest.json").read_text())
            self.assertEqual(saved["status"], "waiting_for_authorization")
            self.assertIn("session_retry_after", saved)

    def test_retry_exception_is_throttled_and_does_not_escape_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, job_dir, _ = self.create_waiting_job(root)
            transient = archive.ArchiveError("channels_backend_unavailable", "offline", 69)
            with patch.object(archive, "inspect_channel_creator", side_effect=transient):
                resumed = archive.resume_waiting_channel_creators_once(root, retry_interval=900)

            self.assertEqual(resumed, 0)
            saved = json.loads((job_dir / "manifest.json").read_text())
            self.assertEqual(saved["status"], "waiting_for_authorization")
            self.assertIn("session_retry_after", saved)
            self.assertEqual(saved["last_retry_error"]["code"], "channels_backend_unavailable")

    def test_retry_throttle_write_error_is_contained(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.create_waiting_job(root)

            with patch.object(archive, "write_json", side_effect=OSError("disk unavailable")):
                resumed = archive.resume_waiting_channel_creators_once(root, retry_interval=900)

            self.assertEqual(resumed, 0)

    def test_worker_skips_corrupt_manifests_and_processes_unrelated_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corrupt_batch = root / "jobs" / "batch-20000101T000000Z-00000000" / "manifest.json"
            corrupt_batch.parent.mkdir(parents=True)
            corrupt_batch.write_text("{not-json", encoding="utf-8")
            corrupt_content = root / "jobs" / "content-20000101T000000Z-00000000" / "manifest.json"
            corrupt_content.parent.mkdir(parents=True)
            corrupt_content.write_text("{not-json", encoding="utf-8")
            _, content_dir, content = archive.new_job(root, "content", "https://www.bilibili.com/video/BV1xx")
            content.update({"platform": "bilibili", "status": "queued"})
            archive.write_json(content_dir / "manifest.json", content)

            with (
                patch.object(archive, "process_content_job", return_value={"status": "completed"}) as process,
                patch.object(archive, "progress_channel_jobs_once"),
            ):
                worker, processed = archive.content_worker_once(root)

            self.assertTrue(processed)
            process.assert_called_once()
            self.assertEqual(worker["status"], "running")

    def test_worker_marks_corrupt_selected_child_failed_and_continues(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, batch_dir, batch = archive.new_job(root, "batch", "https://weixin.qq.com/sph/new")
            child_id = "content-20000101T000000Z-00000000"
            corrupt_child = root / "jobs" / child_id / "manifest.json"
            corrupt_child.parent.mkdir(parents=True)
            corrupt_child.write_text("{not-json", encoding="utf-8")
            batch.update(
                {
                    "kind": "creator_batch",
                    "platform": "wechat_channels",
                    "status": "processing",
                    "selection": {"limit": 1, "order": "newest"},
                    "child_job_ids": [child_id],
                    "inventory": {"items": [{"child_job_id": child_id}]},
                }
            )
            archive.write_json(batch_dir / "manifest.json", batch)
            _, content_dir, content = archive.new_job(root, "content", "https://www.bilibili.com/video/BV1xx")
            content.update({"platform": "bilibili", "status": "queued"})
            archive.write_json(content_dir / "manifest.json", content)

            with (
                patch.object(archive, "process_content_job", return_value={"status": "completed"}) as process,
                patch.object(archive, "progress_channel_jobs_once"),
            ):
                _, processed = archive.content_worker_once(root)

            self.assertTrue(processed)
            process.assert_called_once()
            saved = json.loads((batch_dir / "manifest.json").read_text())
            self.assertEqual(saved["inventory"]["items"][0]["result"], "failed")
            self.assertEqual(saved["inventory"]["items"][0]["error_code"], "child_manifest_invalid")

    def test_content_worker_continues_to_other_jobs_after_creator_retry_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.create_waiting_job(root)
            _, content_dir, content = archive.new_job(root, "content", "https://www.bilibili.com/video/BV1xx")
            content.update({"platform": "bilibili", "status": "queued"})
            archive.write_json(content_dir / "manifest.json", content)
            transient = archive.ArchiveError("channels_backend_unavailable", "offline", 69)
            with (
                patch.object(archive, "inspect_channel_creator", side_effect=transient),
                patch.object(archive, "process_content_job", return_value={"status": "completed"}) as process,
                patch.object(archive, "progress_channel_jobs_once"),
                patch.object(archive, "refresh_creator_batch"),
            ):
                _, processed = archive.content_worker_once(root)

            self.assertTrue(processed)
            process.assert_called_once()


if __name__ == "__main__":
    unittest.main()
