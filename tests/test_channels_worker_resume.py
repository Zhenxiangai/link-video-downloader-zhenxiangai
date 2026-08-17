import importlib.util
import json
import fcntl
import os
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
    def test_creator_plan_lock_prevents_duplicate_child_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job_id, job_dir, manifest = archive.new_job(root, "batch", "https://weixin.qq.com/sph/creator")
            manifest.update(
                {
                    "kind": "creator_batch",
                    "platform": "wechat_channels",
                    "status": "awaiting_download_count",
                    "inventory": {"items": [{"id": "one", "url": "https://weixin.qq.com/sph/one"}]},
                    "child_job_ids": [],
                }
            )
            manifest_path = job_dir / "manifest.json"
            archive.write_json(manifest_path, manifest)
            lock_path = manifest_path.with_suffix(".json.batch.lock")
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch.object(archive, "submit_content") as submit:
                    with self.assertRaises(archive.ArchiveError) as raised:
                        archive.download_creator_plan(job_id, 1, root)
                submit.assert_not_called()
                self.assertEqual(raised.exception.code, "job_busy")
            finally:
                os.close(descriptor)

    def test_creator_recovery_lock_prevents_overlapping_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock_path = root / "state" / "channels-recovery.lock"
            lock_path.parent.mkdir(parents=True)
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch.object(archive, "_resume_waiting_channel_creators_unlocked") as resume:
                    result = archive.resume_waiting_channel_creators_once(root)
                resume.assert_not_called()
                self.assertEqual(result, 0)
            finally:
                os.close(descriptor)
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

    def test_worker_resumes_all_waiting_creators_during_one_session_window(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_id, _, _ = self.create_waiting_job(root)
            second_id, second_dir, second = archive.new_job(root, "batch", "https://weixin.qq.com/sph/second")
            second.update(
                {
                    "kind": "creator_batch",
                    "platform": "wechat_channels",
                    "status": "waiting_for_authorization",
                    "session_retry_after": "2000-01-01T00:00:00Z",
                }
            )
            archive.write_json(second_dir / "manifest.json", second)

            def resumed(url, root, existing_job, deadline=None):
                return {"ok": True, "job_id": existing_job[0], "status": "awaiting_download_count"}

            with patch.object(archive, "_inspect_channel_creator_unlocked", side_effect=resumed) as inspect:
                resumed = archive.resume_waiting_channel_creators_once(root)

            self.assertEqual(resumed, 2)
            self.assertEqual(inspect.call_count, 2)
            resumed_ids = {call.kwargs["existing_job"][0] for call in inspect.call_args_list}
            self.assertEqual(resumed_ids, {first_id, second_id})

    def test_selected_frozen_batch_is_resumed_before_offline_creator_probe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, offline_dir, _ = self.create_waiting_job(root)
            offline_path = offline_dir / "manifest.json"
            offline = json.loads(offline_path.read_text())
            offline["source"] = "https://weixin.qq.com/sph/offline"
            archive.write_json(offline_path, offline)

            _, batch_dir, batch = archive.new_job(root, "batch", "https://weixin.qq.com/sph/frozen")
            child_id, child_dir, child = archive.new_job(root, "content", "https://weixin.qq.com/sph/item")
            child.update({"platform": "wechat_channels", "status": "waiting_for_authorization", "channel_object": {"id": "frozen"}})
            archive.write_json(child_dir / "manifest.json", child)
            batch.update(
                {
                    "kind": "creator_batch",
                    "platform": "wechat_channels",
                    "status": "waiting_for_authorization",
                    "selection": {"limit": 1, "order": "newest"},
                    "child_job_ids": [child_id],
                    "inventory": {"items": [{"child_job_id": child_id}]},
                }
            )
            archive.write_json(batch_dir / "manifest.json", batch)
            unavailable = archive.ArchiveError("channels_authorization_required", "offline", 69)

            with (
                patch.object(archive, "_inspect_channel_creator_unlocked", side_effect=unavailable),
                patch.object(archive, "_refresh_creator_batch_unlocked", side_effect=lambda manifest, path, root: manifest),
            ):
                resumed = archive.resume_waiting_channel_creators_once(root, retry_interval=60)

            self.assertEqual(resumed, 1)
            saved_child = json.loads((child_dir / "manifest.json").read_text())
            self.assertEqual(saved_child["status"], "queued")

    def test_selected_parent_counts_once_even_with_multiple_resumed_children(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, batch_dir, batch = archive.new_job(root, "batch", "https://weixin.qq.com/sph/frozen")
            child_ids = []
            for suffix in ("one", "two"):
                child_id, child_dir, child = archive.new_job(root, "content", f"https://weixin.qq.com/sph/{suffix}")
                child.update({"platform": "wechat_channels", "status": "waiting_for_authorization", "channel_object": {"id": suffix}})
                archive.write_json(child_dir / "manifest.json", child)
                child_ids.append(child_id)
            batch.update(
                {
                    "kind": "creator_batch",
                    "platform": "wechat_channels",
                    "status": "waiting_for_authorization",
                    "selection": {"limit": 2, "order": "newest"},
                    "child_job_ids": child_ids,
                    "inventory": {"items": [{"child_job_id": child_id} for child_id in child_ids]},
                }
            )
            archive.write_json(batch_dir / "manifest.json", batch)
            resumed = archive.resume_waiting_channel_creators_once(root, retry_interval=60)
            self.assertEqual(resumed, 1)

    def test_creator_child_resume_uses_content_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, batch_dir, batch = archive.new_job(root, "batch", "https://weixin.qq.com/sph/frozen")
            child_id, child_dir, child = archive.new_job(root, "content", "https://weixin.qq.com/sph/item")
            child.update({"platform": "wechat_channels", "status": "waiting_for_authorization", "channel_object": {"id": "frozen"}})
            archive.write_json(child_dir / "manifest.json", child)
            batch.update({"child_job_ids": [child_id], "inventory": {"items": [{"child_job_id": child_id}]}})
            archive.write_json(batch_dir / "manifest.json", batch)
            lock_path = child_dir / "manifest.json.lock"
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                _, resumed = archive.resume_creator_batch_children(batch, batch_dir / "manifest.json", root)
            finally:
                os.close(descriptor)
            self.assertEqual(resumed, 0)
            saved = archive.read_json_if_valid(child_dir / "manifest.json") or {}
            self.assertEqual(saved["status"], "waiting_for_authorization")

    def test_legacy_creator_child_is_rehydrated_from_matching_frozen_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, batch_dir, batch = archive.new_job(root, "batch", "https://weixin.qq.com/sph/frozen")
            child_id, child_dir, child = archive.new_job(root, "content", "https://weixin.qq.com/sph/one")
            child.update({"platform": "wechat_channels", "status": "waiting_for_authorization"})
            archive.write_json(child_dir / "manifest.json", child)
            matching = {"id": "one", "objectDesc": {"description": "one"}}
            other = {"id": "two", "objectDesc": {"description": "two"}}
            batch.update(
                {
                    "kind": "creator_batch",
                    "platform": "wechat_channels",
                    "status": "waiting_for_authorization",
                    "selection": {"limit": 2, "order": "newest"},
                    "child_job_ids": [child_id],
                    "inventory": {
                        "items": [
                            {"id": "two", "child_job_id": "content-other", "payload": other},
                            {"id": "one", "child_job_id": child_id, "payload": matching},
                        ]
                    },
                }
            )
            archive.write_json(batch_dir / "manifest.json", batch)

            _, resumed = archive.resume_creator_batch_children(batch, batch_dir / "manifest.json", root)

            self.assertEqual(resumed, 1)
            saved = archive.read_json_if_valid(child_dir / "manifest.json") or {}
            self.assertEqual(saved["status"], "queued")
            self.assertEqual(saved["channel_object"], matching)

    def test_legacy_random_child_id_is_reused_during_partial_submission_upgrade(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent_id, batch_dir, batch = archive.new_job(root, "batch", "https://weixin.qq.com/sph/frozen")
            child_id, child_dir, child = archive.new_job(root, "content", "https://weixin.qq.com/sph/one")
            frozen = {"id": "one", "objectDesc": {"description": "one"}}
            child.update({"platform": "wechat_channels", "status": "queued", "content_id": "one"})
            archive.write_json(child_dir / "manifest.json", child)
            batch.update(
                {
                    "kind": "creator_batch",
                    "platform": "wechat_channels",
                    "status": "submitting",
                    "selection": {"limit": 1, "order": "newest"},
                    "child_job_ids": [child_id],
                    "inventory": {
                        "items": [
                            {
                                "id": "one",
                                "url": "https://weixin.qq.com/sph/one",
                                "title": "one",
                                "payload": frozen,
                                "child_job_id": child_id,
                            }
                        ]
                    },
                }
            )
            manifest_path = batch_dir / "manifest.json"
            archive.write_json(manifest_path, batch)

            archive._submit_creator_batch_children_unlocked(batch, manifest_path, root)

            children = sorted((root / "jobs").glob("content-*/manifest.json"))
            self.assertEqual([path.parent.name for path in children], [child_id])
            saved = archive.read_json_if_valid(child_dir / "manifest.json") or {}
            self.assertEqual(saved["parent_job_id"], parent_id)
            self.assertEqual(saved["channel_object"], frozen)
            self.assertEqual(saved["status"], "downloading")

    def test_submitted_legacy_random_child_is_linked_without_losing_upstream_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent_id, batch_dir, batch = archive.new_job(root, "batch", "https://weixin.qq.com/sph/frozen")
            child_id, child_dir, child = archive.new_job(root, "content", "https://weixin.qq.com/sph/one")
            frozen = {"id": "one", "objectDesc": {"description": "one"}}
            child.update(
                {
                    "platform": "wechat_channels",
                    "status": "downloading",
                    "content_id": "one",
                    "upstream_task_ids": [42],
                }
            )
            archive.write_json(child_dir / "manifest.json", child)
            batch.update(
                {
                    "kind": "creator_batch",
                    "platform": "wechat_channels",
                    "status": "submitting",
                    "selection": {"limit": 1, "order": "newest"},
                    "child_job_ids": [child_id],
                    "inventory": {
                        "items": [
                            {
                                "id": "one",
                                "url": "https://weixin.qq.com/sph/one",
                                "title": "one",
                                "payload": frozen,
                                "child_job_id": child_id,
                            }
                        ]
                    },
                }
            )
            manifest_path = batch_dir / "manifest.json"
            archive.write_json(manifest_path, batch)

            archive._submit_creator_batch_children_unlocked(batch, manifest_path, root)

            saved = archive.read_json_if_valid(child_dir / "manifest.json") or {}
            self.assertEqual(saved["parent_job_id"], parent_id)
            self.assertEqual(saved["channel_object"], frozen)
            self.assertEqual(saved["upstream_task_ids"], [42])
            self.assertEqual(saved["status"], "downloading")

    def test_deterministic_child_creation_recovers_from_empty_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent_id, _, _ = archive.new_job(root, "batch", "https://weixin.qq.com/sph/frozen")
            item = {
                "id": "one",
                "url": "https://weixin.qq.com/sph/one",
                "title": "one",
                "payload": {"id": "one", "objectDesc": {"description": "one"}},
            }
            child_id = archive.frozen_channel_child_job_id(parent_id, item)
            (root / "jobs" / child_id).mkdir()

            result = archive.submit_frozen_channel_content(item, root, parent_id)

            self.assertEqual(result["job_id"], child_id)
            saved = archive.read_json_if_valid(root / "jobs" / child_id / "manifest.json") or {}
            self.assertEqual(saved["status"], "staged")
            self.assertEqual(saved["channel_object"], item["payload"])

    def test_explicit_recovery_window_ignores_old_retry_deadlines_and_drains_backlog(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, first_dir, first = self.create_waiting_job(root)
            first["session_retry_after"] = "2999-01-01T00:00:00Z"
            archive.write_json(first_dir / "manifest.json", first)
            _, second_dir, second = archive.new_job(root, "batch", "https://weixin.qq.com/sph/second")
            second.update(
                {
                    "kind": "creator_batch",
                    "platform": "wechat_channels",
                    "status": "waiting_for_authorization",
                    "session_retry_after": "2999-01-01T00:00:00Z",
                }
            )
            archive.write_json(second_dir / "manifest.json", second)

            with (
                patch.object(archive, "channel_session_status", return_value={"ok": True, "status": "author_search_ready"}),
                patch.object(
                    archive,
                    "_inspect_channel_creator_unlocked",
                    side_effect=lambda url, root, existing_job, deadline=None: {
                        "ok": True,
                        "job_id": existing_job[0],
                        "status": "awaiting_download_count",
                    },
                ) as inspect,
            ):
                result = archive.recover_channel_session(root, timeout=30, poll_interval=1)

            self.assertTrue(result["ok"])
            self.assertEqual(result["resumed_creator_batches"], 2)
            self.assertEqual(inspect.call_count, 2)

    def test_explicit_recovery_window_starts_all_waiting_single_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job_ids = []
            for suffix in ("one", "two"):
                job_id, job_dir, manifest = archive.new_job(root, "content", f"https://weixin.qq.com/sph/{suffix}")
                manifest.update({"platform": "wechat_channels", "status": "waiting_for_authorization"})
                archive.write_json(job_dir / "manifest.json", manifest)
                job_ids.append(job_id)

            def start_job(path, root, resume_waiting=False, deadline=None, start_only=False):
                manifest = json.loads(path.read_text())
                manifest.update({"status": "downloading", "upstream_task_ids": [len(started) + 1]})
                archive.write_json(path, manifest)
                started.append(manifest["job_id"])
                return manifest

            started = []
            with (
                patch.object(archive, "channel_session_status", return_value={"ok": True, "status": "author_search_ready"}),
                patch.object(archive, "process_content_job", side_effect=start_job),
            ):
                result = archive.recover_channel_session(root, timeout=30, poll_interval=1)

            self.assertEqual(result["resumed_content_jobs"], 2)
            self.assertEqual(set(started), set(job_ids))
            for job_id in job_ids:
                saved = json.loads((root / "jobs" / job_id / "manifest.json").read_text())
                self.assertEqual(saved["status"], "downloading")

    def test_recovery_window_processes_frozen_children_requeued_by_creator_batch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, child_dir, child = archive.new_job(root, "content", "https://weixin.qq.com/sph/frozen")
            child.update({"platform": "wechat_channels", "status": "queued", "channel_object": {"id": "frozen"}})
            archive.write_json(child_dir / "manifest.json", child)
            processed = []

            def process(path, root, resume_waiting=False, deadline=None, start_only=False):
                processed.append(path.parent.name)
                saved = archive.read_json_if_valid(path) or {}
                saved["status"] = "downloading"
                archive.write_json(path, saved)
                return saved

            with (
                patch.object(archive, "channel_session_status", return_value={"ok": True, "status": "author_search_ready"}),
                patch.object(archive, "resume_waiting_channel_creators_once", return_value=1),
                patch.object(archive, "process_content_job", side_effect=process),
            ):
                result = archive.recover_channel_session(root, timeout=30, poll_interval=5)

            self.assertEqual(result["resumed_content_jobs"], 1)
            self.assertEqual(processed, [child_dir.name])

    def test_recovery_window_starts_frozen_downloading_child_without_upstream_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, child_dir, child = archive.new_job(root, "content", "https://weixin.qq.com/sph/frozen")
            child.update({"platform": "wechat_channels", "status": "downloading", "channel_object": {"id": "frozen"}})
            archive.write_json(child_dir / "manifest.json", child)
            with patch.object(archive, "process_content_job", return_value={"status": "downloading"}) as process:
                resumed = archive.resume_waiting_channel_content(
                    root,
                    deadline=archive.time.monotonic() + 30.0,
                )
            self.assertEqual(resumed, 1)
            process.assert_called_once()
            self.assertTrue(process.call_args.kwargs["start_only"])

    def test_existing_creator_inspection_revalidates_state_after_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job_id, job_dir, stale = self.create_waiting_job(root)
            current = dict(stale)
            current.update({"status": "processing", "selection": {"limit": 1}, "child_job_ids": ["content-20000101T000000Z-00000000"]})
            archive.write_json(job_dir / "manifest.json", current)
            with patch.object(archive, "_inspect_channel_creator_unlocked") as inspect:
                result = archive.inspect_channel_creator(
                    str(stale["source"]),
                    root,
                    existing_job=(job_id, job_dir, stale),
                )
            inspect.assert_not_called()
            self.assertEqual(result["status"], "processing")

    def test_recovery_deadline_is_forwarded_to_both_drain_loops(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch.object(archive, "channel_session_status", return_value={"ok": True, "status": "author_search_ready"}),
                patch.object(archive, "resume_waiting_channel_creators_once", return_value=0) as creators,
                patch.object(archive, "resume_waiting_channel_content", return_value=0) as content,
                patch.object(archive.time, "monotonic", side_effect=[100.0, 101.0, 101.0]),
            ):
                archive.recover_channel_session(root, timeout=30, poll_interval=5)
            self.assertEqual(creators.call_args.kwargs["deadline"], 130.0)
            self.assertEqual(content.call_args.kwargs["deadline"], 130.0)

    def test_recovery_deadline_is_forwarded_to_session_probe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch.object(archive, "channel_session_status", return_value={"ok": True}) as status,
                patch.object(archive, "resume_waiting_channel_creators_once", return_value=0),
                patch.object(archive, "resume_waiting_channel_content", return_value=0),
                patch.object(archive.time, "monotonic", return_value=100.0),
            ):
                archive.recover_channel_session(root, timeout=30, poll_interval=5)
            self.assertEqual(status.call_args.kwargs["deadline"], 130.0)

    def test_explicit_recovery_reports_busy_creator_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock_path = root / "state" / "channels-recovery.lock"
            lock_path.parent.mkdir(parents=True)
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(archive.ArchiveError) as raised:
                    archive.resume_waiting_channel_creators_once(root, ignore_retry_after=True, deadline=130.0)
            finally:
                os.close(descriptor)
            self.assertEqual(raised.exception.code, "recovery_window_busy")

    def test_recovery_does_not_report_success_when_deadline_expires_after_probe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch.object(archive, "channel_session_status", return_value={"ok": True, "status": "author_search_ready"}),
                patch.object(archive, "resume_waiting_channel_creators_once") as creators,
                patch.object(archive, "resume_waiting_channel_content") as content,
                patch.object(archive.time, "monotonic", side_effect=[100.0, 130.0]),
            ):
                result = archive.recover_channel_session(root, timeout=30, poll_interval=5)
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "recovery_window_expired")
            creators.assert_not_called()
            content.assert_not_called()

    def test_recovery_does_not_report_success_when_deadline_expires_after_drain(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch.object(archive, "channel_session_status", return_value={"ok": True, "status": "author_search_ready"}),
                patch.object(archive, "resume_waiting_channel_creators_once", return_value=1),
                patch.object(archive, "resume_waiting_channel_content", return_value=0),
                patch.object(archive.time, "monotonic", side_effect=[100.0, 101.0, 130.0]),
            ):
                result = archive.recover_channel_session(root, timeout=30, poll_interval=5)
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "recovery_window_expired")
            self.assertEqual(result["resumed_creator_batches"], 1)

    def test_failed_content_job_is_not_counted_as_resumed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, job_dir, manifest = archive.new_job(root, "content", "https://weixin.qq.com/sph/fail")
            manifest.update({"platform": "wechat_channels", "status": "waiting_for_authorization"})
            archive.write_json(job_dir / "manifest.json", manifest)
            with patch.object(archive, "process_content_job", return_value={"status": "failed"}):
                resumed = archive.resume_waiting_channel_content(root)
            self.assertEqual(resumed, 0)

    def test_failed_creator_job_is_not_counted_as_resumed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.create_waiting_job(root)
            failed = {"ok": False, "status": "failed"}
            with patch.object(archive, "_inspect_channel_creator_unlocked", return_value=failed):
                resumed = archive.resume_waiting_channel_creators_once(root, retry_interval=60)
            self.assertEqual(resumed, 0)

    def test_creator_discovery_deadline_stops_before_next_page(self):
        selected = {"username": "creator@finder", "nickname": "creator"}
        first_page = {"object": [{"id": "one", "objectDesc": {"media": []}}], "lastBuffer": "next", "continueFlag": 1}
        with (
            patch.object(archive, "channels_api", return_value={"data": first_page}) as api,
            patch.object(archive.time, "monotonic", side_effect=[100.0, 131.0]),
        ):
            with self.assertRaises(archive.ArchiveError) as raised:
                archive.discover_channel_author(selected, deadline=130.0)
        self.assertEqual(raised.exception.code, "recovery_window_expired")
        self.assertEqual(api.call_count, 1)

    def test_job_status_does_not_refresh_creator_parent_without_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job_id, job_dir, manifest = archive.new_job(root, "batch", "https://weixin.qq.com/sph/creator")
            manifest.update({"kind": "creator_batch", "status": "processing", "inventory": {"items": []}})
            manifest_path = job_dir / "manifest.json"
            archive.write_json(manifest_path, manifest)
            descriptor = archive.acquire_creator_batch_lock(manifest_path)
            self.assertIsNotNone(descriptor)
            try:
                with patch.object(archive, "_refresh_creator_batch_unlocked") as refresh:
                    result = archive.job_status(job_id, root)
                refresh.assert_not_called()
                self.assertEqual(result["job"]["status"], "processing")
            finally:
                os.close(descriptor)

    def test_existing_creator_inspection_does_not_write_when_parent_lock_is_busy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job_id, job_dir, manifest = self.create_waiting_job(root)
            manifest_path = job_dir / "manifest.json"
            descriptor = archive.acquire_creator_batch_lock(manifest_path)
            self.assertIsNotNone(descriptor)
            try:
                with patch.object(archive, "_inspect_channel_creator_unlocked") as inspect:
                    result = archive.inspect_channel_creator(
                        str(manifest["source"]),
                        root,
                        existing_job=(job_id, job_dir, manifest),
                    )
                inspect.assert_not_called()
                self.assertEqual(result["status"], "waiting_for_authorization")
            finally:
                os.close(descriptor)

    def test_channels_creator_child_is_never_exposed_runnable_without_frozen_object(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, job_dir, manifest = archive.new_job(root, "batch", "https://weixin.qq.com/sph/creator")
            frozen = {"id": "one", "objectDesc": {"mediaType": 4, "media": [{"url": "https://example.invalid/one"}]}}
            manifest.update(
                {
                    "kind": "creator_batch",
                    "platform": "wechat_channels",
                    "status": "submitting",
                    "selection": {"limit": 1},
                    "inventory": {"items": [{"id": "one", "url": "https://weixin.qq.com/sph/one", "title": "one", "payload": frozen}]},
                    "child_job_ids": [],
                }
            )
            manifest_path = job_dir / "manifest.json"
            archive.write_json(manifest_path, manifest)
            observed = []
            original_write = archive.write_json

            def record(path, value):
                if path.parent.name.startswith("content-"):
                    observed.append(json.loads(json.dumps(value)))
                original_write(path, value)

            with patch.object(archive, "write_json", side_effect=record):
                archive._submit_creator_batch_children_unlocked(manifest, manifest_path, root)

            runnable = [item for item in observed if item.get("status") in {"queued", "downloading", "transcribing"}]
            self.assertTrue(runnable)
            self.assertTrue(all(item.get("channel_object") == frozen for item in runnable))

    def test_creator_child_is_linked_in_parent_before_becoming_runnable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, job_dir, manifest = archive.new_job(root, "batch", "https://weixin.qq.com/sph/creator")
            frozen = {"id": "one", "objectDesc": {"description": "one"}}
            manifest.update(
                {
                    "kind": "creator_batch",
                    "platform": "wechat_channels",
                    "status": "submitting",
                    "selection": {"limit": 1},
                    "inventory": {"items": [{"id": "one", "url": "https://weixin.qq.com/sph/one", "title": "one", "payload": frozen}]},
                    "child_job_ids": [],
                }
            )
            manifest_path = job_dir / "manifest.json"
            archive.write_json(manifest_path, manifest)
            original_write = archive.write_json

            def verify_parent_link(path, value):
                if path.parent.name.startswith("content-") and value.get("status") == "downloading":
                    parent = archive.read_json_if_valid(manifest_path) or {}
                    self.assertIn(value["job_id"], parent.get("child_job_ids") or [])
                original_write(path, value)

            with patch.object(archive, "write_json", side_effect=verify_parent_link):
                archive._submit_creator_batch_children_unlocked(manifest, manifest_path, root)

    def test_creator_child_retry_reuses_staged_id_after_parent_link_write_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, job_dir, manifest = archive.new_job(root, "batch", "https://weixin.qq.com/sph/creator")
            frozen = {"id": "one", "objectDesc": {"description": "one"}}
            manifest.update(
                {
                    "kind": "creator_batch",
                    "platform": "wechat_channels",
                    "status": "submitting",
                    "selection": {"limit": 1},
                    "inventory": {"items": [{"id": "one", "url": "https://weixin.qq.com/sph/one", "title": "one", "payload": frozen}]},
                    "child_job_ids": [],
                }
            )
            manifest_path = job_dir / "manifest.json"
            archive.write_json(manifest_path, manifest)
            original_write = archive.write_json

            def fail_parent_once(path, value):
                if path == manifest_path and value.get("child_job_ids"):
                    raise OSError("simulated crash before parent link")
                original_write(path, value)

            with patch.object(archive, "write_json", side_effect=fail_parent_once):
                with self.assertRaises(OSError):
                    archive._submit_creator_batch_children_unlocked(manifest, manifest_path, root)
            staged = sorted((root / "jobs").glob("content-*/manifest.json"))
            self.assertEqual(len(staged), 1)
            staged_id = staged[0].parent.name
            self.assertEqual((archive.read_json_if_valid(staged[0]) or {}).get("status"), "staged")

            current = archive.read_json_if_valid(manifest_path) or {}
            archive._submit_creator_batch_children_unlocked(current, manifest_path, root)

            children = sorted((root / "jobs").glob("content-*/manifest.json"))
            self.assertEqual([path.parent.name for path in children], [staged_id])
            parent = archive.read_json_if_valid(manifest_path) or {}
            self.assertEqual(parent["child_job_ids"], [staged_id])
            self.assertEqual((archive.read_json_if_valid(children[0]) or {}).get("status"), "downloading")

    def test_recovery_window_reuses_one_unique_probe_across_polls(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            probes = []

            def status(root, probe_keyword=None, deadline=None):
                probes.append(probe_keyword)
                if len(probes) == 1:
                    return {"ok": False, "status": "authorization_required"}
                return {"ok": True, "status": "author_search_ready", "last_realtime_ready_at": "now"}

            with (
                patch.object(archive, "channel_session_status", side_effect=status),
                patch.object(archive, "resume_waiting_channel_creators_once", return_value=0),
                patch.object(archive, "resume_waiting_channel_content", return_value=0),
                patch.object(archive.time, "sleep"),
            ):
                result = archive.recover_channel_session(root, timeout=30, poll_interval=5)

            self.assertTrue(result["ok"])
            self.assertEqual(len(probes), 2)
            self.assertEqual(probes[0], probes[1])
            self.assertTrue(probes[0].startswith("__hermes_session_probe_"))

    def test_worker_stops_backlog_probe_when_realtime_session_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.create_waiting_job(root)
            _, second_dir, second = archive.new_job(root, "batch", "https://weixin.qq.com/sph/second")
            second.update(
                {
                    "kind": "creator_batch",
                    "platform": "wechat_channels",
                    "status": "waiting_for_authorization",
                    "session_retry_after": "2000-01-01T00:00:00Z",
                }
            )
            archive.write_json(second_dir / "manifest.json", second)
            waiting = {"ok": True, "status": "waiting_for_authorization"}

            with patch.object(archive, "_inspect_channel_creator_unlocked", return_value=waiting) as inspect:
                resumed = archive.resume_waiting_channel_creators_once(root)

            self.assertEqual(resumed, 0)
            inspect.assert_called_once()

    def test_worker_throttles_session_retries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, job_dir, manifest = self.create_waiting_job(root)
            manifest["session_retry_after"] = (datetime.now(timezone.utc) + timedelta(minutes=10)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            archive.write_json(job_dir / "manifest.json", manifest)
            with patch.object(archive, "_inspect_channel_creator_unlocked") as inspect:
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
                    "inventory": {
                        "items": [
                            {
                                "child_job_id": child_id,
                                "payload": {"id": "child", "objectDesc": {"description": "child"}},
                            }
                        ]
                    },
                }
            )
            archive.write_json(job_dir / "manifest.json", manifest)

            with patch.object(archive, "_inspect_channel_creator_unlocked") as inspect:
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
            with patch.object(archive, "_inspect_channel_creator_unlocked", return_value=waiting):
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
            with patch.object(archive, "_inspect_channel_creator_unlocked", side_effect=transient):
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
                patch.object(archive, "_inspect_channel_creator_unlocked", side_effect=transient),
                patch.object(archive, "process_content_job", return_value={"status": "completed"}) as process,
                patch.object(archive, "progress_channel_jobs_once"),
                patch.object(archive, "_refresh_creator_batch_unlocked"),
            ):
                _, processed = archive.content_worker_once(root)

            self.assertTrue(processed)
            process.assert_called_once()


if __name__ == "__main__":
    unittest.main()
