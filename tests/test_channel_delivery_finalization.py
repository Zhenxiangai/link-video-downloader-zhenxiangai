from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "wechat_archive.py"
SPEC = importlib.util.spec_from_file_location("wechat_archive_channel_delivery", MODULE_PATH)
assert SPEC and SPEC.loader
archive = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(archive)


class ChannelDeliveryFinalizationTests(unittest.TestCase):
    def make_job(self, root: Path, *, status: str = "downloading", error_code: str | None = None):
        job_id = "content-20000101T000000Z-00000000"
        job_dir = root / "jobs" / job_id
        work_dir = job_dir / "work"
        work_dir.mkdir(parents=True)
        manifest = {
            "job_id": job_id,
            "kind": "content",
            "platform": "wechat_channels",
            "status": status,
            "source": "https://weixin.qq.com/sph/example",
            "content_id": "channel-object-one",
            "title": "测试视频",
            "channel_object": {"id": "channel-object-one"},
            "upstream_task_ids": [42],
        }
        if error_code:
            manifest.update(
                {
                    "completed_at": "2026-08-17T00:00:00Z",
                    "failed_stage": "downloading",
                    "error": {"code": error_code, "message": "previous failure"},
                }
            )
        manifest_path = job_dir / "manifest.json"
        archive.write_json(manifest_path, manifest)
        return manifest, manifest_path, work_dir

    @staticmethod
    def completed_task(_path: str, **_kwargs):
        return {"code": 0, "data": {"status": 5}}

    def test_uses_unique_mp4_from_job_work_directory_when_backend_path_is_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, manifest_path, work_dir = self.make_job(root)
            local_video = work_dir / "downloaded.mp4"
            local_video.write_bytes(b"video")
            records = [
                {
                    "status": 5,
                    "files": [
                        {
                            "download_dir": str(work_dir),
                            "name": "not-yet-visible.mp4",
                        }
                    ],
                }
            ]

            with (
                patch.object(archive, "channels_api", side_effect=self.completed_task),
                patch.object(archive, "channel_task_records", return_value=records),
                patch.object(archive, "ensure_video_readable"),
                patch.object(archive, "transcribe_content_video", return_value=[]),
            ):
                result = archive.process_channel_content(manifest, manifest_path, root)

            self.assertEqual(result["status"], "completed")
            self.assertFalse(local_video.exists())
            video_output = next(output for output in result["outputs"] if output["role"] == "video")
            self.assertTrue((root / video_output["path"]).is_file())

    def test_waits_for_delayed_mp4_visibility_after_backend_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, manifest_path, work_dir = self.make_job(root)
            local_video = work_dir / "delayed.mp4"
            records = [
                {
                    "status": 5,
                    "files": [
                        {"download_dir": str(work_dir), "name": local_video.name}
                    ],
                }
            ]

            def publish_video(_seconds: float):
                if not local_video.exists():
                    local_video.write_bytes(b"video")

            with (
                patch.object(archive, "channels_api", side_effect=self.completed_task),
                patch.object(archive, "channel_task_records", return_value=records),
                patch.object(archive.time, "sleep", side_effect=publish_video) as sleep,
                patch.object(archive, "ensure_video_readable"),
                patch.object(archive, "transcribe_content_video", return_value=[]),
            ):
                result = archive.process_channel_content(manifest, manifest_path, root)

            self.assertEqual(result["status"], "completed")
            sleep.assert_called()
            self.assertFalse(local_video.exists())

    def test_failed_missing_video_is_recovered_from_existing_job_file_without_backend_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, manifest_path, work_dir = self.make_job(
                root,
                status="failed",
                error_code="channel_video_missing",
            )
            local_video = work_dir / "already-downloaded.mp4"
            local_video.write_bytes(b"video")

            with (
                patch.object(archive, "channels_api", side_effect=AssertionError("backend must not be called")),
                patch.object(archive, "ensure_video_readable"),
                patch.object(archive, "transcribe_content_video", return_value=[]),
            ):
                _worker, processed = archive.content_worker_once(root)
                _worker, processed_again = archive.content_worker_once(root)

            self.assertTrue(processed)
            self.assertFalse(processed_again)
            self.assertFalse(local_video.exists())
            saved = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "completed")
            self.assertNotIn("error", saved)
            self.assertIn("completed_at", saved)
            video_output = next(output for output in saved["outputs"] if output["role"] == "video")
            self.assertTrue((root / video_output["path"]).is_file())

    def test_does_not_guess_when_work_directory_contains_multiple_mp4_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, manifest_path, work_dir = self.make_job(root)
            (work_dir / "one.mp4").write_bytes(b"one")
            (work_dir / "two.mp4").write_bytes(b"two")

            with (
                patch.object(archive, "channels_api", side_effect=self.completed_task),
                patch.object(
                    archive,
                    "channel_task_records",
                    return_value=[{"status": 5, "files": []}],
                ),
                patch.object(archive, "CHANNEL_VIDEO_FINALIZE_WAIT_SECONDS", 0, create=True),
            ):
                with self.assertRaises(archive.ArchiveError) as raised:
                    archive.process_channel_content(manifest, manifest_path, root)

            self.assertEqual(raised.exception.code, "channel_video_ambiguous")

    def test_backend_file_outside_job_work_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "archive"
            root.mkdir()
            manifest, manifest_path, _work_dir = self.make_job(root)
            outside = base / "outside.mp4"
            outside.write_bytes(b"video")
            records = [
                {
                    "status": 5,
                    "files": [
                        {"download_dir": str(outside.parent), "name": outside.name}
                    ],
                }
            ]

            with (
                patch.object(archive, "channel_task_records", return_value=records),
                patch.object(archive, "CHANNEL_VIDEO_FINALIZE_WAIT_SECONDS", 0),
            ):
                with self.assertRaises(archive.ArchiveError) as raised:
                    archive.process_channel_content(manifest, manifest_path, root)

            self.assertEqual(raised.exception.code, "channel_video_missing")
            self.assertTrue(outside.is_file())

    def test_waits_until_visible_mp4_size_is_stable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, manifest_path, work_dir = self.make_job(root)
            local_video = work_dir / "growing.mp4"
            local_video.write_bytes(b"one")
            records = [
                {
                    "status": 5,
                    "files": [
                        {"download_dir": str(work_dir), "name": local_video.name}
                    ],
                }
            ]
            sleep_count = 0

            def finish_write(_seconds: float):
                nonlocal sleep_count
                sleep_count += 1
                if sleep_count == 1:
                    local_video.write_bytes(b"finished-video")

            with (
                patch.object(archive, "channel_task_records", return_value=records),
                patch.object(archive.time, "sleep", side_effect=finish_write),
                patch.object(archive, "ensure_video_readable"),
                patch.object(archive, "transcribe_content_video", return_value=[]),
            ):
                result = archive.process_channel_content(manifest, manifest_path, root)

            self.assertEqual(result["status"], "completed")
            self.assertGreaterEqual(sleep_count, 2)
            video_output = next(output for output in result["outputs"] if output["role"] == "video")
            self.assertEqual((root / video_output["path"]).read_bytes(), b"finished-video")

    def test_recovered_child_reconciles_terminal_creator_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, manifest_path, work_dir = self.make_job(
                root,
                status="failed",
                error_code="channel_video_missing",
            )
            (work_dir / "downloaded.mp4").write_bytes(b"video")
            parent_id = "batch-20000101T000000Z-11111111"
            manifest["parent_job_id"] = parent_id
            archive.write_json(manifest_path, manifest)
            parent_dir = root / "jobs" / parent_id
            parent_dir.mkdir(parents=True)
            parent_path = parent_dir / "manifest.json"
            parent = {
                "job_id": parent_id,
                "kind": "creator_batch",
                "platform": "wechat_channels",
                "status": "completed_with_failures",
                "selection": {"limit": 1, "order": "newest"},
                "child_job_ids": [manifest["job_id"]],
                "inventory": {
                    "items": [
                        {
                            "id": manifest["content_id"],
                            "child_job_id": manifest["job_id"],
                            "result": "failed",
                            "error_code": "channel_video_missing",
                        }
                    ]
                },
                "counts": {"selected": 1, "completed": 0, "failed": 1},
            }
            archive.write_json(parent_path, parent)

            with (
                patch.object(archive, "ensure_video_readable"),
                patch.object(archive, "transcribe_content_video", return_value=[]),
            ):
                _worker, processed = archive.content_worker_once(root)

            self.assertTrue(processed)
            saved_parent = json.loads(parent_path.read_text(encoding="utf-8"))
            self.assertEqual(saved_parent["status"], "completed")
            self.assertEqual(saved_parent["counts"]["completed"], 1)
            self.assertEqual(saved_parent["counts"]["failed"], 0)
            self.assertEqual(saved_parent["inventory"]["items"][0]["result"], "completed")
            self.assertNotIn("error_code", saved_parent["inventory"]["items"][0])

    def test_local_recovery_survives_crash_after_move_before_manifest_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, manifest_path, work_dir = self.make_job(
                root,
                status="failed",
                error_code="channel_video_missing",
            )
            source = work_dir / "downloaded.mp4"
            source.write_bytes(b"video")
            real_write_json = archive.write_json
            crashed = False

            def crash_before_transcribing_write(path, value):
                nonlocal crashed
                if value.get("status") == "transcribing" and not crashed:
                    crashed = True
                    raise SystemExit("simulated crash")
                return real_write_json(path, value)

            with (
                patch.object(archive, "ensure_video_readable"),
                patch.object(archive, "transcribe_content_video", return_value=[]),
                patch.object(archive.time, "sleep", return_value=None),
                patch.object(archive, "write_json", side_effect=crash_before_transcribing_write),
            ):
                with self.assertRaises(SystemExit):
                    archive.process_content_job(manifest_path, root, resume_delivery=True)

            interrupted = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(interrupted["status"], "downloading")
            self.assertTrue(interrupted["channel_delivery_recovery"])
            self.assertFalse(source.exists())
            archived_candidates = list((root / "content" / "视频号").glob("*/video.mp4"))
            self.assertEqual(len(archived_candidates), 1)
            archived = archived_candidates[0]
            self.assertTrue(archived.is_file())

            with (
                patch.object(archive, "ensure_video_readable"),
                patch.object(archive, "transcribe_content_video", return_value=[]),
                patch.object(archive.time, "sleep", return_value=None),
                patch.object(archive, "channels_api", side_effect=AssertionError("backend must not be called")),
            ):
                completed = archive.process_content_job(manifest_path, root)

            self.assertEqual(completed["status"], "completed")
            self.assertNotIn("channel_delivery_recovery", completed)
            self.assertTrue(archived.is_file())

    def test_worker_recovers_600_failed_deliveries_once_without_duplicates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            expected = []
            for index in range(600):
                job_id = f"content-20000101T000000Z-{index:08x}"
                job_dir = root / "jobs" / job_id
                work_dir = job_dir / "work"
                work_dir.mkdir(parents=True)
                (work_dir / "downloaded.mp4").write_bytes(b"video")
                manifest = {
                    "schema_version": 1,
                    "tool_version": archive.VERSION,
                    "job_id": job_id,
                    "kind": "content",
                    "status": "failed",
                    "platform": "wechat_channels",
                    "source": f"https://weixin.qq.com/sph/{index}",
                    "content_id": str(index),
                    "title": f"video-{index}",
                    "upstream_task_ids": [index + 1],
                    "error": {
                        "code": "channel_video_missing",
                        "message": "missing",
                    },
                }
                archive.write_json(job_dir / "manifest.json", manifest)
                expected.append(job_id)

            with (
                patch.object(archive, "ensure_video_readable") as readable,
                patch.object(archive, "transcribe_content_video", return_value=[]),
                patch.object(archive.time, "sleep", return_value=None),
                patch.object(archive, "channels_api", side_effect=AssertionError("backend must not be called")),
            ):
                for _ in expected:
                    _, processed = archive.content_worker_once(root)
                    self.assertTrue(processed)
                _, processed = archive.content_worker_once(root)

            self.assertFalse(processed)
            self.assertEqual(readable.call_count, 600)
            output_paths = set()
            for job_id in expected:
                saved = json.loads((root / "jobs" / job_id / "manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["status"], "completed")
                self.assertNotIn("error", saved)
                video_output = next(output for output in saved["outputs"] if output["role"] == "video")
                output_paths.add(video_output["path"])
                self.assertTrue((root / video_output["path"]).is_file())
            self.assertEqual(len(output_paths), 600)


if __name__ == "__main__":
    unittest.main()
