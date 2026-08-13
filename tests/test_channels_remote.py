import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "wechat_archive.py"
SPEC = importlib.util.spec_from_file_location("wechat_archive", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load wechat_archive module")
archive = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(archive)


CREATOR = {
    "username": "creator@finder",
    "nickname": "新博主",
    "avatar": "https://example.com/avatar.jpg",
    "signature": "简介",
}
OBJECT = {
    "id": "object-1",
    "createtime": 123,
    "contact": {"nickname": "新博主"},
    "objectDesc": {"mediaType": archive.CHANNEL_VIDEO_MEDIA_TYPE, "media": [{"url": "https://example.com/video"}], "description": "第一条"},
}


class ChannelsRemoteAccessTests(unittest.TestCase):
    def test_new_share_link_reuses_existing_session_without_opening_wechat(self):
        api_payload = {"errCode": 0, "data": {"object": {"contact": CREATOR}}}
        with (
            patch.object(archive, "resolve_channel_share_metadata", return_value={"eid": "eid-1", "nickname": "新博主", "avatar": CREATOR["avatar"]}),
            patch.object(archive, "channels_api", return_value=api_payload) as api,
            patch.object(archive.subprocess, "run") as run,
        ):
            creator = archive.resolve_channel_author_from_url("https://weixin.qq.com/sph/example")

        self.assertEqual(creator["username"], "creator@finder")
        api.assert_called_once_with("/api/channels/feed/profile", query={"eid": "eid-1"}, deadline=None)
        run.assert_not_called()

    def test_direct_profile_same_nickname_allows_avatar_cdn_variant(self):
        mismatch = {**CREATOR, "headUrl": "https://example.com/different.jpg"}
        api_payload = {"errCode": 0, "data": {"object": {"contact": mismatch}}}
        with (
            patch.object(archive, "resolve_channel_share_metadata", return_value={"eid": "eid-1", "nickname": "新博主", "avatar": CREATOR["avatar"]}),
            patch.object(archive, "channels_api", return_value=api_payload),
        ):
            creator = archive.resolve_channel_author_from_url("https://weixin.qq.com/sph/example")

        self.assertEqual(creator["username"], "creator@finder")

    def test_direct_profile_same_nickname_allows_missing_profile_avatar(self):
        missing = {**CREATOR, "headUrl": "", "avatar": ""}
        api_payload = {"errCode": 0, "data": {"object": {"contact": missing}}}
        with (
            patch.object(archive, "resolve_channel_share_metadata", return_value={"eid": "eid-1", "nickname": "新博主", "avatar": CREATOR["avatar"]}),
            patch.object(archive, "channels_api", return_value=api_payload),
        ):
            creator = archive.resolve_channel_author_from_url("https://weixin.qq.com/sph/example")

        self.assertEqual(creator["username"], "creator@finder")

    def test_direct_profile_same_nickname_allows_missing_public_avatar(self):
        api_payload = {"errCode": 0, "data": {"object": {"contact": CREATOR}}}
        with (
            patch.object(archive, "resolve_channel_share_metadata", return_value={"eid": "eid-1", "nickname": "新博主", "avatar": ""}),
            patch.object(archive, "channels_api", return_value=api_payload),
        ):
            creator = archive.resolve_channel_author_from_url("https://weixin.qq.com/sph/example")

        self.assertEqual(creator["username"], "creator@finder")

    def test_direct_profile_nickname_mismatch_never_auto_binds(self):
        mismatch = {**CREATOR, "nickname": "另一个博主"}
        api_payload = {"errCode": 0, "data": {"object": {"contact": mismatch}}}
        with (
            patch.object(archive, "resolve_channel_share_metadata", return_value={"eid": "eid-1", "nickname": "新博主", "avatar": CREATOR["avatar"]}),
            patch.object(archive, "channels_api", return_value=api_payload),
        ):
            with self.assertRaises(archive.ArchiveError) as raised:
                archive.resolve_channel_author_from_url("https://weixin.qq.com/sph/example")

        self.assertEqual(raised.exception.code, "channel_author_selection_required")

    def test_missing_public_avatar_never_auto_binds_search_result(self):
        unavailable = archive.ArchiveError("channels_authorization_required", "profile unavailable", 69)
        with (
            patch.object(archive, "resolve_channel_share_metadata", return_value={"eid": "eid-1", "nickname": "新博主", "avatar": ""}),
            patch.object(archive, "channels_api", side_effect=unavailable),
            patch.object(archive, "search_channel_author", return_value={"ok": True, "count": 1, "candidates": [CREATOR]}),
        ):
            with self.assertRaises(archive.ArchiveError) as raised:
                archive.resolve_channel_author_from_url("https://weixin.qq.com/sph/example")

        self.assertEqual(raised.exception.code, "channel_author_selection_required")

    def test_new_share_falls_back_to_public_author_and_existing_search_session(self):
        unavailable = archive.ArchiveError("channels_authorization_required", "profile unavailable", 69)
        with (
            patch.object(archive, "resolve_channel_share_metadata", return_value={"eid": "eid-1", "nickname": "新博主", "avatar": CREATOR["avatar"]}),
            patch.object(archive, "channels_api", side_effect=unavailable),
            patch.object(archive, "search_channel_author", return_value={"ok": True, "count": 1, "candidates": [CREATOR]}) as search,
            patch.object(archive.subprocess, "run") as run,
        ):
            creator = archive.resolve_channel_author_from_url("https://weixin.qq.com/sph/example")

        self.assertEqual(creator["username"], "creator@finder")
        search.assert_called_once_with("新博主", deadline=None)
        run.assert_not_called()

    def test_public_author_fallback_uses_avatar_to_disambiguate_same_name(self):
        unavailable = archive.ArchiveError("channels_authorization_required", "profile unavailable", 69)
        other = {**CREATOR, "username": "other@finder", "avatar": "https://example.com/other.jpg"}
        with (
            patch.object(archive, "resolve_channel_share_metadata", return_value={"eid": "eid-1", "nickname": "新博主", "avatar": CREATOR["avatar"]}),
            patch.object(archive, "channels_api", side_effect=unavailable),
            patch.object(archive, "search_channel_author", return_value={"ok": True, "count": 2, "candidates": [other, CREATOR]}),
        ):
            creator = archive.resolve_channel_author_from_url("https://weixin.qq.com/sph/example")

        self.assertEqual(creator["username"], "creator@finder")

    def test_public_avatar_mismatch_never_auto_binds_same_name(self):
        unavailable = archive.ArchiveError("channels_authorization_required", "profile unavailable", 69)
        mismatch = {**CREATOR, "avatar": "https://example.com/different.jpg"}
        with (
            patch.object(archive, "resolve_channel_share_metadata", return_value={"eid": "eid-1", "nickname": "新博主", "avatar": CREATOR["avatar"]}),
            patch.object(archive, "channels_api", side_effect=unavailable),
            patch.object(archive, "search_channel_author", return_value={"ok": True, "count": 1, "candidates": [mismatch]}),
        ):
            with self.assertRaises(archive.ArchiveError) as raised:
                archive.resolve_channel_author_from_url("https://weixin.qq.com/sph/example")

        self.assertEqual(raised.exception.code, "channel_author_selection_required")

    def test_session_failure_never_requests_hermes_to_operate_wechat(self):
        unavailable = archive.ArchiveError("channels_authorization_required", "视频号采集会话尚未就绪。", 69)
        with (
            patch.object(archive, "resolve_channel_share_metadata", return_value={"eid": "eid-1", "nickname": "", "avatar": ""}),
            patch.object(archive, "channels_api", side_effect=unavailable),
            patch.object(archive.subprocess, "run") as run,
        ):
            with self.assertRaises(archive.ArchiveError) as raised:
                archive.resolve_channel_author_from_url("https://weixin.qq.com/sph/example")

        self.assertEqual(raised.exception.code, "channels_authorization_required")
        self.assertIn("在 Mac 微信中手动打开", str(raised.exception))
        self.assertNotIn("Computer Use", str(raised.exception))
        run.assert_not_called()

    def test_creator_registry_reuses_known_share_without_profile_lookup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive.register_channel_creator(root, "https://weixin.qq.com/sph/example", CREATOR)
            with patch.object(archive, "resolve_channel_author_from_url") as profile:
                creator = archive.resolve_channel_author("https://weixin.qq.com/sph/example", root)

            self.assertEqual(creator["username"], "creator@finder")
            profile.assert_not_called()
            registry = json.loads((root / "state" / "channels-creators.json").read_text())
            self.assertEqual(registry["sources"]["https://weixin.qq.com/sph/example"], "creator@finder")

    def test_unavailable_new_creator_is_persisted_as_resumable_job(self):
        unavailable = archive.ArchiveError("channels_authorization_required", "offline", 69)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(archive, "resolve_channel_author", side_effect=unavailable):
                result = archive.inspect_channel_creator("https://weixin.qq.com/sph/new", root)

            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "waiting_for_authorization")
            manifest_path = root / result["manifest"]
            manifest = json.loads(manifest_path.read_text())
            self.assertEqual(manifest["source"], "https://weixin.qq.com/sph/new")
            self.assertEqual(manifest["status"], "waiting_for_authorization")
            self.assertIn("手动打开", manifest["next_action"])
            self.assertNotIn("Hermes", manifest["next_action"])

    def test_creator_feed_backend_error_is_persisted_as_resumable_job(self):
        transient = archive.ArchiveError("channels_backend_error", "feed unavailable", 69)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch.object(archive, "resolve_channel_author", return_value=CREATOR),
                patch.object(archive, "discover_channel_author", side_effect=transient),
            ):
                result = archive.inspect_channel_creator("https://weixin.qq.com/sph/new", root)
            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "waiting_for_authorization")
            manifest = json.loads((root / result["manifest"]).read_text())
            self.assertNotIn("completed_at", manifest)
            self.assertEqual(manifest["error"]["code"], "channels_backend_error")

    def test_non_retryable_inventory_failure_is_persisted_with_job_id(self):
        failure = archive.ArchiveError("channel_author_selection_required", "ambiguous", 66)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(archive, "resolve_channel_author", side_effect=failure):
                result = archive.inspect_channel_creator("https://weixin.qq.com/sph/new", root)

            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "failed")
            self.assertTrue(result["job_id"].startswith("batch-"))
            manifest = json.loads((root / result["manifest"]).read_text())
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["error"]["code"], "channel_author_selection_required")

    def test_empty_inventory_is_persisted_as_failed_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch.object(archive, "resolve_channel_author", return_value=CREATOR),
                patch.object(archive, "discover_channel_author", return_value=([], 0)),
            ):
                result = archive.inspect_channel_creator("https://weixin.qq.com/sph/empty", root)

            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "failed")
            manifest = json.loads((root / result["manifest"]).read_text())
            self.assertEqual(manifest["error"]["code"], "creator_inventory_empty")

    def test_resume_waiting_creator_uses_same_job_and_builds_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job_id, job_dir, manifest = archive.new_job(root, "batch", "https://weixin.qq.com/sph/new")
            manifest.update({"kind": "creator_batch", "platform": "wechat_channels", "status": "waiting_for_authorization"})
            archive.write_json(job_dir / "manifest.json", manifest)
            with (
                patch.object(archive, "resolve_channel_author", return_value=CREATOR),
                patch.object(archive, "discover_channel_author", return_value=([[OBJECT]], 1)),
            ):
                result = archive.resume_job(job_id, root)

            self.assertEqual(result["job_id"], job_id)
            self.assertEqual(result["status"], "awaiting_download_count")
            saved = json.loads((job_dir / "manifest.json").read_text())
            self.assertEqual(saved["inventory"]["available"], 1)
            self.assertEqual(saved["creator"]["id"], "creator@finder")

    def test_manual_resume_selected_creator_batch_preserves_existing_children(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job_id, job_dir, manifest = archive.new_job(root, "batch", "https://weixin.qq.com/sph/new")
            child_id, child_dir, child = archive.new_job(root, "content", "https://weixin.qq.com/sph/child")
            child.update({"platform": "wechat_channels", "status": "waiting_for_authorization"})
            archive.write_json(child_dir / "manifest.json", child)
            manifest.update(
                {
                    "kind": "creator_batch",
                    "platform": "wechat_channels",
                    "status": "waiting_for_authorization",
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

            with (
                patch.object(archive, "inspect_channel_creator") as inspect,
                patch.object(archive, "_refresh_creator_batch_unlocked", return_value=manifest) as refresh,
            ):
                result = archive.resume_job(job_id, root)

            inspect.assert_not_called()
            refresh.assert_called_once()
            self.assertEqual(result["job_id"], job_id)
            saved = json.loads((job_dir / "manifest.json").read_text())
            self.assertEqual(saved["selection"]["limit"], 1)
            self.assertEqual(saved["child_job_ids"], [child_id])
            resumed_child = json.loads((child_dir / "manifest.json").read_text())
            self.assertEqual(resumed_child["status"], "queued")

    def test_single_channel_link_registers_creator_for_later_batch_use(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job_id, job_dir, manifest = archive.new_job(root, "content", "https://weixin.qq.com/sph/one")
            manifest.update({"platform": "wechat_channels", "status": "queued"})
            archive.write_json(job_dir / "manifest.json", manifest)
            with (
                patch.object(archive, "submit_content", return_value={"job_id": job_id}),
                patch.object(archive, "resolve_channel_author_from_url", return_value=CREATOR),
                patch.object(archive, "process_content_job", return_value={"status": "completed"}),
            ):
                result = archive.download_channel_url("https://weixin.qq.com/sph/one", root)

            self.assertTrue(result["ok"])
            registry = archive.load_channel_creator_registry(root)
            self.assertEqual(registry["sources"]["https://weixin.qq.com/sph/one"], "creator@finder")

    def test_single_channel_author_backend_transient_is_resumable(self):
        transient = archive.ArchiveError("channels_backend_error", "temporary", 69)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            submitted = archive.submit_content("https://weixin.qq.com/sph/one", root)
            with (
                patch.object(archive, "submit_content", return_value=submitted),
                patch.object(archive, "resolve_channel_author_from_url", side_effect=transient),
            ):
                result = archive.download_channel_url("https://weixin.qq.com/sph/one", root)

            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "waiting_for_authorization")
            manifest = archive.read_json_if_valid(root / result["manifest"]) or {}
            self.assertNotIn("completed_at", manifest)
            self.assertEqual(manifest["error"]["code"], "channels_backend_error")

    def test_creator_registry_is_private_from_first_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive.register_channel_creator(root, "https://weixin.qq.com/sph/one", CREATOR)
            path = archive.channel_creator_registry_path(root)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(path.parent).st_mode & 0o777, 0o700)

    def test_session_status_is_read_only_and_reports_ready(self):
        with patch.object(archive, "search_channel_author", return_value={"ok": True, "count": 0, "candidates": []}) as search:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                status = archive.channel_session_status(root)
                archive.channel_session_status(root)
        self.assertEqual(status["status"], "author_search_ready")
        self.assertFalse(status["capabilities"]["creator_feed"])
        queries = [call.args[0] for call in search.call_args_list]
        self.assertEqual(len(queries), 2)
        self.assertNotEqual(queries[0], queries[1])
        self.assertTrue(all(query.startswith("__hermes_session_probe_") for query in queries))

    def test_session_status_marks_authorization_required_not_ready(self):
        unavailable = archive.ArchiveError("channels_authorization_required", "offline", 69)
        with patch.object(archive, "search_channel_author", side_effect=unavailable):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                archive.register_channel_creator(root, "https://weixin.qq.com/sph/one", CREATOR)
                status = archive.channel_session_status(root)
                snapshot = archive.load_channel_session_snapshot(root)
        self.assertFalse(status["ok"])
        self.assertEqual(status["status"], "authorization_required")
        self.assertEqual(status["realtime_status"], "authorization_required")
        self.assertFalse(status["capabilities"]["realtime_author_search"])
        self.assertTrue(status["capabilities"]["registered_creator_lookup"])
        self.assertEqual(status["registered_creators"], 1)
        self.assertEqual(snapshot["realtime_status"], "authorization_required")
        self.assertEqual(snapshot["registered_creators"], 1)

    def test_session_snapshot_preserves_last_ready_time_after_disconnect(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(archive, "search_channel_author", return_value={"ok": True, "count": 0, "candidates": []}):
                archive.channel_session_status(root)
            ready = archive.load_channel_session_snapshot(root)
            unavailable = archive.ArchiveError("channels_authorization_required", "offline", 69)
            with patch.object(archive, "search_channel_author", side_effect=unavailable):
                archive.channel_session_status(root)
            disconnected = archive.load_channel_session_snapshot(root)

        self.assertEqual(disconnected["last_ready_at"], ready["last_ready_at"])
        self.assertEqual(disconnected["realtime_status"], "authorization_required")

    def test_session_snapshot_is_private_and_telemetry_failure_is_nonfatal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive.save_channel_session_snapshot(root, "ready", 2)
            path = archive.channel_session_snapshot_path(root)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with patch.object(archive, "save_channel_session_snapshot", side_effect=OSError("readonly")):
                with patch.object(archive, "search_channel_author", return_value={"ok": True, "count": 0, "candidates": []}):
                    status = archive.channel_session_status(root)
            self.assertTrue(status["ok"])

    def test_malformed_session_snapshot_types_fall_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = archive.channel_session_snapshot_path(root)
            path.parent.mkdir(parents=True)
            path.write_text('{"registered_creators": "not-a-number", "realtime_status": []}', encoding="utf-8")
            snapshot = archive.load_channel_session_snapshot(root)
            self.assertEqual(snapshot["registered_creators"], 0)
            self.assertEqual(snapshot["realtime_status"], "unknown")


if __name__ == "__main__":
    unittest.main()
