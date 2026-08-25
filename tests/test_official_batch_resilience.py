import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "wechat_archive.py"
SPEC = importlib.util.spec_from_file_location("wechat_archive_official", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load wechat_archive module")
archive = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(archive)


class OfficialBatchResilienceTests(unittest.TestCase):
    def test_official_account_directory_requires_stable_identity(self):
        with self.assertRaisesRegex(archive.ArchiveError, "稳定账号标识"):
            archive.official_account_dir({"name": "同名公众号"})

    def test_finalize_official_article_groups_output_by_account(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job_dir = root / "jobs" / "content-20000101T000000Z-00000000"
            job_dir.mkdir(parents=True)
            (job_dir / "original.html").write_text("<html></html>", encoding="utf-8")
            (job_dir / "article.md").write_text("# 正文", encoding="utf-8")
            manifest_path = job_dir / "manifest.json"
            manifest = {
                "job_id": job_dir.name,
                "kind": "content",
                "platform": "wechat_official_account",
                "status": "downloading",
                "content_id": "article-one",
                "title": "测试文章",
                "account": {"name": "测试公众号", "account_id": "account-one"},
            }

            completed = archive.finalize_official_article(
                manifest,
                manifest_path,
                root,
                "https://mp.weixin.qq.com/s?__biz=biz-one&mid=1&idx=1&sn=test",
            )

            expected = "content/公众号/测试公众号--account-one/测试文章--article-one"
            self.assertEqual(completed["output_dir"], expected)
            self.assertTrue((root / expected / "original.html").is_file())
            self.assertTrue((root / expected / "正文.md").is_file())

    def test_discovery_persists_biz_across_pages(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job_id, job_dir, manifest = archive.new_job(root, "batch", "https://mp.weixin.qq.com/s/example")
            manifest.update(
                {
                    "platform": "wechat_official_account",
                    "account": {"name": "", "account_id": ""},
                    "pagination": {"pages": 0, "next_offset": 0, "can_continue": True, "complete": False},
                    "counts": {"discovered": 0},
                    "items": [],
                }
            )
            manifest_path = job_dir / "manifest.json"
            archive.write_json(manifest_path, manifest)
            reference = {"biz": "biz-id", "account_name": "测试公众号", "account_id": "account-id"}
            page = {"can_msg_continue": 1, "next_offset": 10}
            first_channels_api = MagicMock(return_value=page)
            with (
                patch.object(archive, "fetch_limited", return_value=(b"<html></html>", "text/html", manifest["source"])),
                patch.object(archive, "official_article_metadata", return_value=reference),
                patch.object(archive, "channels_api", first_channels_api),
                patch.object(archive, "official_batch_page_items", return_value=[]),
            ):
                updated = archive.discover_official_batch(manifest, manifest_path, root)

            first_channels_api.assert_called_once_with(
                "/api/mp/msg/list", query={"biz": "biz-id", "offset": 0}
            )
            self.assertEqual(updated["account"]["biz"], "biz-id")
            persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["account"]["biz"], "biz-id")

            empty_reference = {"biz": "", "account_name": "", "account_id": ""}
            final_page = {"can_msg_continue": 0, "next_offset": 0}
            resumed_channels_api = MagicMock(return_value=final_page)
            with (
                patch.object(archive, "fetch_limited", return_value=(b"<html></html>", "text/html", manifest["source"])),
                patch.object(archive, "official_article_metadata", return_value=empty_reference),
                patch.object(archive, "channels_api", resumed_channels_api),
                patch.object(archive, "official_batch_page_items", return_value=[]),
            ):
                resumed = archive.discover_official_batch(persisted, manifest_path, root)

            resumed_channels_api.assert_called_once_with(
                "/api/mp/msg/list", query={"biz": "biz-id", "offset": 10}
            )
            self.assertEqual(resumed["account"]["biz"], "biz-id")
            self.assertEqual(resumed["status"], "awaiting_download_count")
            self.assertTrue(resumed["pagination"]["complete"])

    def test_mp_api_token_prefers_neutral_auth_file_name_and_keeps_legacy_compatibility(self):
        with tempfile.TemporaryDirectory() as temporary:
            auth_file = Path(temporary) / "auth-file"
            legacy_file = Path(temporary) / "legacy-file"
            auth_file.write_text("auth-value\n", encoding="utf-8")
            legacy_file.write_text("legacy-value\n", encoding="utf-8")

            with patch.dict(
                os.environ,
                {
                    "WECHAT_MP_AUTH_FILE": str(auth_file),
                    "WECHAT_MP_TOKEN_FILE": str(legacy_file),
                },
            ):
                self.assertEqual(archive.mp_api_token(), "auth-value")

            with patch.dict(os.environ, {"WECHAT_MP_TOKEN_FILE": str(legacy_file)}, clear=True):
                self.assertEqual(archive.mp_api_token(), "legacy-value")

    def build_official_history_db(self, path: Path, accounts: list[tuple[str, str, str, int]]) -> None:
        with sqlite3.connect(path) as connection:
            connection.executescript(
                """
                CREATE TABLE browse_history (id TEXT PRIMARY KEY, type TEXT, url TEXT, updated_at INTEGER);
                CREATE TABLE browse_history_account (browse_history_id TEXT, account_id TEXT, role TEXT);
                CREATE TABLE account (id TEXT PRIMARY KEY, external_id TEXT, nickname TEXT);
                """
            )
            for index, (biz, external_id, nickname, updated_at) in enumerate(accounts):
                history_id = f"history-{index}"
                account_id = f"account-{index}"
                connection.execute(
                    "INSERT INTO browse_history VALUES (?, 'article', ?, ?)",
                    (history_id, f"https://mp.weixin.qq.com/s?__biz={biz}&mid=1&idx=1&sn=test", updated_at),
                )
                connection.execute("INSERT INTO account VALUES (?, ?, ?)", (account_id, external_id, nickname))
                connection.execute(
                    "INSERT INTO browse_history_account VALUES (?, ?, 'author')",
                    (history_id, account_id),
                )

    def test_known_official_account_accepts_exact_source_match(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "data.db"
            self.build_official_history_db(database, [("biz-one", "gh_one", "唯一作者", 2_000)])
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "ALTER TABLE browse_history ADD COLUMN source_url TEXT"
                )
                connection.execute(
                    "UPDATE browse_history SET source_url = ?",
                    ("https://mp.weixin.qq.com/s/exact-source",),
                )
            with patch.dict(os.environ, {"WECHAT_CHANNELS_DATA_DB": str(database)}):
                account = archive.known_official_account_for_source(
                    "https://mp.weixin.qq.com/s/exact-source", Path(temporary)
                )

            self.assertEqual(account["biz"], "biz-one")
            self.assertEqual(account["account_name"], "唯一作者")
            self.assertTrue(account["account_id"])

    def test_known_official_account_ignores_other_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "data.db"
            self.build_official_history_db(database, [("biz-one", "gh_one", "其他作者", 2_000)])
            with sqlite3.connect(database) as connection:
                connection.execute("ALTER TABLE browse_history ADD COLUMN source_url TEXT")
                connection.execute(
                    "UPDATE browse_history SET source_url = ?",
                    ("https://mp.weixin.qq.com/s/other-source",),
                )
            with patch.dict(os.environ, {"WECHAT_CHANNELS_DATA_DB": str(database)}):
                self.assertIsNone(
                    archive.known_official_account_for_source(
                        "https://mp.weixin.qq.com/s/exact-source", Path(temporary)
                    )
                )

    def test_dedup_scan_skips_unrelated_corrupt_content_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corrupt = root / "jobs" / "content-20000101T000000Z-00000000" / "manifest.json"
            corrupt.parent.mkdir(parents=True)
            corrupt.write_text("{not-json", encoding="utf-8")

            self.assertIsNone(archive.existing_official_content(root, "article-1"))

    def test_dedup_scan_skips_non_utf8_content_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corrupt = root / "jobs" / "content-20000101T000000Z-00000000" / "manifest.json"
            corrupt.parent.mkdir(parents=True)
            corrupt.write_bytes(b"\xff\xfe\xfd")

            self.assertIsNone(archive.existing_official_content(root, "article-1"))

    def test_refresh_marks_corrupt_selected_child_failed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job_id, job_dir, manifest = archive.new_job(root, "batch", "https://mp.weixin.qq.com/s/example")
            child_id = "content-20000101T000000Z-00000000"
            corrupt = root / "jobs" / child_id / "manifest.json"
            corrupt.parent.mkdir(parents=True)
            corrupt.write_text("{not-json", encoding="utf-8")
            manifest.update(
                {
                    "kind": "batch",
                    "platform": "wechat_official_account",
                    "status": "processing",
                    "selection": {"limit": 1, "order": "newest"},
                    "items": [{"content_id": "article-1", "child_job_id": child_id, "result": "processing"}],
                }
            )
            archive.write_json(job_dir / "manifest.json", manifest)

            refreshed = archive.refresh_official_batch(manifest, job_dir / "manifest.json", root)

            self.assertEqual(refreshed["items"][0]["result"], "failed")
            self.assertEqual(refreshed["items"][0]["error_code"], "child_manifest_invalid")
            self.assertEqual(refreshed["status"], "completed_with_failures")

    def test_refresh_reclassifies_legacy_empty_article_as_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent_id = "batch-20000101T000000Z-00000000"
            child_id = "content-20000101T000000Z-00000001"
            parent_path = root / "jobs" / parent_id / "manifest.json"
            child_path = root / "jobs" / child_id / "manifest.json"
            parent_path.parent.mkdir(parents=True)
            child_path.parent.mkdir(parents=True)
            archive.write_json(
                child_path,
                {
                    "job_id": child_id,
                    "parent_job_id": parent_id,
                    "platform": "wechat_official_account",
                    "status": "failed",
                    "error": {"code": "article_not_found", "message": "empty article"},
                },
            )
            archive.write_json(
                parent_path,
                {
                    "job_id": parent_id,
                    "kind": "batch",
                    "platform": "wechat_official_account",
                    "status": "processing",
                    "selection": {"limit": 1},
                    "items": [{"content_id": "article-1", "child_job_id": child_id, "result": "processing"}],
                },
            )

            refreshed = archive.refresh_official_batch(
                json.loads(parent_path.read_text(encoding="utf-8")), parent_path, root
            )

            self.assertEqual(json.loads(child_path.read_text(encoding="utf-8"))["status"], "unavailable")
            self.assertEqual(refreshed["items"][0]["result"], "unavailable")
            self.assertEqual(refreshed["counts"]["unavailable"], 1)
            self.assertEqual(refreshed["counts"]["failed"], 0)

    def test_refresh_moves_legacy_completed_output_into_account_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent_id = "batch-20000101T000000Z-00000000"
            child_id = "content-20000101T000000Z-00000001"
            parent_path = root / "jobs" / parent_id / "manifest.json"
            child_path = root / "jobs" / child_id / "manifest.json"
            legacy_dir = root / "content" / "公众号" / "测试文章--article-one"
            legacy_dir.mkdir(parents=True)
            original = legacy_dir / "original.html"
            original.write_text("unchanged", encoding="utf-8")
            archive.write_json(
                child_path,
                {
                    "job_id": child_id,
                    "parent_job_id": parent_id,
                    "platform": "wechat_official_account",
                    "status": "completed",
                    "content_id": "article-one",
                    "output_dir": archive.archive_relative(root, legacy_dir),
                    "outputs": [archive.output_record(root, original, "original_html")],
                },
            )
            archive.write_json(
                parent_path,
                {
                    "job_id": parent_id,
                    "kind": "batch",
                    "platform": "wechat_official_account",
                    "status": "processing",
                    "account": {"name": "测试公众号", "account_id": "account-one"},
                    "selection": {"limit": 1},
                    "items": [{"content_id": "article-one", "child_job_id": child_id, "result": "processing"}],
                },
            )

            archive.refresh_official_batch(
                json.loads(parent_path.read_text(encoding="utf-8")), parent_path, root
            )

            expected = root / "content" / "公众号" / "测试公众号--account-one" / legacy_dir.name
            saved = json.loads(child_path.read_text(encoding="utf-8"))
            self.assertFalse(legacy_dir.exists())
            self.assertEqual(saved["output_dir"], archive.archive_relative(root, expected))
            self.assertEqual(saved["outputs"][0]["path"], archive.archive_relative(root, expected / "original.html"))
            self.assertEqual((expected / "original.html").read_text(encoding="utf-8"), "unchanged")

    def test_legacy_output_migration_validates_records_before_move(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy_dir = root / "content" / "公众号" / "测试文章--article-one"
            legacy_dir.mkdir(parents=True)
            manifest = {
                "output_dir": archive.archive_relative(root, legacy_dir),
                "outputs": [{"path": "content/公众号/其他文章/file"}],
            }

            with self.assertRaisesRegex(archive.ArchiveError, "输出记录不属于文章目录"):
                archive.group_legacy_official_output(
                    manifest, root, {"name": "测试公众号", "account_id": "account-one"}
                )

            self.assertTrue(legacy_dir.is_dir())

    def test_legacy_output_migration_rejects_unverified_recovery_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = Path("content/公众号/测试文章--article-one")
            target = root / "content" / "公众号" / "测试公众号--account-one" / legacy.name
            target.mkdir(parents=True)
            manifest = {
                "output_dir": legacy.as_posix(),
                "outputs": [
                    {
                        "path": f"{legacy.as_posix()}/original.html",
                        "bytes": 9,
                        "sha256": archive.hashlib.sha256(b"unchanged").hexdigest(),
                    }
                ],
            }

            with self.assertRaisesRegex(archive.ArchiveError, "迁移目标校验失败"):
                archive.group_legacy_official_output(
                    manifest, root, {"name": "测试公众号", "account_id": "account-one"}
                )

            self.assertEqual(manifest["output_dir"], legacy.as_posix())

    def test_legacy_output_migration_rejects_account_directory_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "archive"
            outside = base / "outside"
            outside.mkdir()
            legacy_dir = root / "content" / "公众号" / "测试文章--article-one"
            legacy_dir.mkdir(parents=True)
            original = legacy_dir / "original.html"
            original.write_text("unchanged", encoding="utf-8")
            account_dir = root / "content" / "公众号" / "测试公众号--account-one"
            account_dir.symlink_to(outside, target_is_directory=True)
            manifest = {
                "output_dir": archive.archive_relative(root, legacy_dir),
                "outputs": [archive.output_record(root, original, "original_html")],
            }

            with self.assertRaisesRegex(archive.ArchiveError, "归档根目录之外"):
                archive.group_legacy_official_output(
                    manifest, root, {"name": "测试公众号", "account_id": "account-one"}
                )

            self.assertTrue(legacy_dir.is_dir())
            self.assertFalse((outside / legacy_dir.name).exists())

    def test_refresh_migrates_skipped_existing_completed_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent_id = "batch-20000101T000000Z-00000000"
            child_id = "content-20000101T000000Z-00000001"
            parent_path = root / "jobs" / parent_id / "manifest.json"
            child_path = root / "jobs" / child_id / "manifest.json"
            legacy_dir = root / "content" / "公众号" / "测试文章--article-one"
            legacy_dir.mkdir(parents=True)
            original = legacy_dir / "original.html"
            original.write_text("unchanged", encoding="utf-8")
            archive.write_json(
                child_path,
                {
                    "job_id": child_id,
                    "status": "completed",
                    "output_dir": archive.archive_relative(root, legacy_dir),
                    "outputs": [archive.output_record(root, original, "original_html")],
                },
            )
            parent = {
                "job_id": parent_id,
                "kind": "batch",
                "platform": "wechat_official_account",
                "status": "processing",
                "account": {"name": "测试公众号", "account_id": "account-one"},
                "selection": {"limit": 1},
                "items": [{"child_job_id": child_id, "result": "skipped_existing"}],
            }
            archive.write_json(parent_path, parent)

            refreshed = archive.refresh_official_batch(parent, parent_path, root)

            target = root / "content" / "公众号" / "测试公众号--account-one" / legacy_dir.name
            self.assertTrue((target / "original.html").is_file())
            self.assertEqual(refreshed["items"][0]["result"], "skipped_existing")
            self.assertEqual(refreshed["counts"]["skipped_existing"], 1)

    def test_process_official_article_uses_local_session_backend(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job_dir = root / "jobs" / "content-20000101T000000Z-00000000"
            job_dir.mkdir(parents=True)
            manifest_path = job_dir / "manifest.json"
            source = "https://mp.weixin.qq.com/s?__biz=biz-one&mid=1&idx=1&sn=test"
            manifest = {
                "job_id": job_dir.name,
                "kind": "content",
                "status": "downloading",
                "platform": "wechat_official_account",
                "source": source,
            }
            archive.write_json(manifest_path, manifest)
            article = {
                "content_id": "article-one",
                "canonical_url": source,
                "published_at": "2026-08-12T12:00:00Z",
            }
            with (
                patch.object(archive, "fetch_official_article_with_session", return_value=(b"<html>article</html>", "text/html", source)) as session_fetch,
                patch.object(archive, "official_article_metadata", return_value=article),
                patch.object(archive, "archive_article_html") as archive_html,
                patch.object(archive, "finalize_official_article", return_value={"status": "completed"}) as finalize,
            ):
                result = archive.process_official_article(manifest, manifest_path, root)

            self.assertEqual(result["status"], "completed")
            self.assertEqual(manifest["published_at"], "2026-08-12T12:00:00Z")
            session_fetch.assert_called_once_with(source)
            archive_html.assert_called_once()
            finalize.assert_called_once()

    def test_official_fetch_failure_retries_current_child_before_pausing_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent_id = "batch-20000101T000000Z-00000000"
            child_ids = ["content-20000101T000000Z-00000001", "content-20000101T000000Z-00000002"]
            parent_path = root / "jobs" / parent_id / "manifest.json"
            parent_path.parent.mkdir(parents=True)
            parent = {
                "job_id": parent_id,
                "kind": "batch",
                "platform": "wechat_official_account",
                "status": "processing",
                "selection": {"limit": 2, "order": "newest"},
                "items": [
                    {"content_id": f"article-{index}", "child_job_id": child_id, "result": "processing"}
                    for index, child_id in enumerate(child_ids)
                ],
            }
            archive.write_json(parent_path, parent)
            for child_id in child_ids:
                child_path = root / "jobs" / child_id / "manifest.json"
                child_path.parent.mkdir(parents=True)
                archive.write_json(
                    child_path,
                    {
                        "job_id": child_id,
                        "kind": "content",
                        "platform": "wechat_official_account",
                        "parent_job_id": parent_id,
                        "status": "queued",
                        "source": "https://mp.weixin.qq.com/s?__biz=biz&mid=1&idx=1&sn=test",
                    },
                )

            failure = archive.ArchiveError("official_article_fetch_failed", "公众号正文读取失败。", 69)
            with patch.object(archive, "process_official_article", side_effect=failure):
                first = archive.process_content_job(root / "jobs" / child_ids[0] / "manifest.json", root)
            self.assertEqual(first["status"], "queued")
            self.assertEqual(first["official_fetch_retries"], 1)
            self.assertEqual(json.loads(parent_path.read_text(encoding="utf-8"))["status"], "processing")

            with patch.object(archive, "process_official_article") as process_article:
                archive.content_worker_once(root)
            process_article.assert_called_once()
            self.assertEqual(process_article.call_args.args[0]["job_id"], child_ids[0])
            second = json.loads((root / "jobs" / child_ids[1] / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(second["status"], "queued")

            with patch.object(archive, "process_official_article", side_effect=failure):
                for _ in range(archive.OFFICIAL_FETCH_MAX_RETRIES):
                    final = archive.process_content_job(root / "jobs" / child_ids[0] / "manifest.json", root)
            self.assertEqual(final["status"], "waiting_for_reauthentication")
            self.assertEqual(json.loads(parent_path.read_text(encoding="utf-8"))["status"], "waiting_for_reauthentication")

    def test_official_backend_unavailable_requests_local_service_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent_id = "batch-20000101T000000Z-00000000"
            child_id = "content-20000101T000000Z-00000001"
            parent_path = root / "jobs" / parent_id / "manifest.json"
            child_path = root / "jobs" / child_id / "manifest.json"
            archive.write_json(
                parent_path,
                {
                    "job_id": parent_id,
                    "kind": "batch",
                    "platform": "wechat_official_account",
                    "status": "processing",
                    "selection": {"limit": 1},
                    "items": [{"child_job_id": child_id, "result": "processing"}],
                },
            )
            archive.write_json(
                child_path,
                {
                    "job_id": child_id,
                    "kind": "content",
                    "platform": "wechat_official_account",
                    "parent_job_id": parent_id,
                    "status": "queued",
                    "source": "https://mp.weixin.qq.com/s?__biz=biz&mid=1&idx=1&sn=test",
                },
            )
            unavailable = archive.ArchiveError("channels_backend_unavailable", "backend down", 69)

            with patch.object(archive, "process_official_article", side_effect=unavailable):
                child = archive.process_content_job(child_path, root)

            parent = json.loads(parent_path.read_text(encoding="utf-8"))
            self.assertEqual(child["status"], "waiting_for_authorization")
            self.assertIn("本地会话后端", child["next_action"])
            self.assertNotIn("打开任意一篇", child["next_action"])
            self.assertEqual(parent["status"], "waiting_for_authorization")

    def test_resume_official_batch_requeues_retryable_children_in_place(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent_id = "batch-20000101T000000Z-00000000"
            child_ids = ["content-20000101T000000Z-00000001", "content-20000101T000000Z-00000002"]
            parent_path = root / "jobs" / parent_id / "manifest.json"
            parent_path.parent.mkdir(parents=True)
            archive.write_json(
                parent_path,
                {
                    "job_id": parent_id,
                    "kind": "batch",
                    "platform": "wechat_official_account",
                    "status": "completed_with_failures",
                    "completed_at": "2000-01-01T00:00:00Z",
                    "selection": {"limit": 2, "order": "newest"},
                    "items": [
                        {
                            "content_id": f"article-{index}",
                            "child_job_id": child_id,
                            "result": "failed",
                            "error_code": "official_article_fetch_failed",
                        }
                        for index, child_id in enumerate(child_ids)
                    ],
                },
            )
            for child_id in child_ids:
                child_path = root / "jobs" / child_id / "manifest.json"
                child_path.parent.mkdir(parents=True)
                archive.write_json(
                    child_path,
                    {
                        "job_id": child_id,
                        "kind": "content",
                        "platform": "wechat_official_account",
                        "parent_job_id": parent_id,
                        "status": "failed",
                        "completed_at": "2000-01-01T00:00:00Z",
                        "official_fetch_retries": 7,
                        "error": {"code": "official_article_fetch_failed", "message": "failed"},
                    },
                )

            result = archive.resume_job(parent_id, root)

            self.assertEqual(result["resumed_jobs"], 2)
            self.assertEqual(result["status"], "processing")
            saved_parent = json.loads(parent_path.read_text(encoding="utf-8"))
            self.assertNotIn("completed_at", saved_parent)
            self.assertEqual([item["child_job_id"] for item in saved_parent["items"]], child_ids)
            for child_id in child_ids:
                child = json.loads((root / "jobs" / child_id / "manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(child["status"], "queued")
                self.assertNotIn("error", child)
                self.assertNotIn("official_fetch_retries", child)

    def test_content_worker_sleeps_only_when_idle(self):
        worker = {"counts": {}}
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(archive, "content_worker_once", side_effect=[(worker, True), (worker, False)]) as run_once,
            patch.object(archive.time, "sleep", side_effect=RuntimeError("stop")) as sleep,
        ):
            with self.assertRaisesRegex(RuntimeError, "stop"):
                archive.watch_content(Path(temporary), interval=10, once=False)

        self.assertEqual(run_once.call_count, 2)
        sleep.assert_called_once_with(10)

    def test_session_backend_request_uses_non_browser_local_header(self):
        source = "https://mp.weixin.qq.com/s?__biz=biz-one&mid=1&idx=1&sn=test"
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers.get_content_type.return_value = "text/html"
        response.headers.get.return_value = None
        response.read.return_value = b"<html></html>"
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(archive, "build_opener", return_value=opener):
            archive.fetch_official_article_with_session(source)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("X-wxmp-local-client"), "1")
        self.assertIsNone(request.get_header("Origin"))

    def test_official_article_metadata_extracts_wechat_publish_timestamp(self):
        source = "https://mp.weixin.qq.com/s?__biz=biz-one&mid=1&idx=1&sn=test"
        metadata = archive.official_article_metadata(source, b'<script>var ct = "1786536000";</script>')
        self.assertEqual(metadata["published_at"], "2026-08-12T12:00:00Z")

    def test_archive_article_markdown_includes_inventory_publish_time(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job_dir = root / "jobs" / "content-20000101T000000Z-00000000"
            job_dir.mkdir(parents=True)
            manifest_path = job_dir / "manifest.json"
            source = "https://mp.weixin.qq.com/s?__biz=biz-one&mid=1&idx=1&sn=test"
            manifest = {
                "job_id": job_dir.name,
                "kind": "content",
                "status": "downloading",
                "source": source,
                "published_at": "2026-08-12T12:00:00Z",
            }
            html_body = b"<html><head><meta property='og:title' content='test'></head><body><div id='js_content'>article body text long enough</div></body></html>"
            archive.archive_article_html(
                source,
                html_body,
                root,
                job_context=(job_dir.name, job_dir, manifest),
            )
            markdown = (job_dir / "article.md").read_text(encoding="utf-8")
            self.assertIn("- 发布日期：2026-08-12T12:00:00Z", markdown)

    def test_archive_article_upgrades_exact_wechat_image_host_to_https(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = "https://mp.weixin.qq.com/s?__biz=biz-one&mid=1&idx=1&sn=test"
            html_body = b"""
                <html><head><meta property='og:title' content='test'></head>
                <body><div id='js_content'>article body text long enough
                <img data-src='http://mmbiz.qpic.cn/sz_mmbiz_png/example/640'/>
                </div></body></html>
            """
            requested = []

            def media_fetcher(url):
                requested.append(url)
                return b"image", "image/png", url

            result = archive.archive_article_html(source, html_body, root, media_fetcher=media_fetcher)
            manifest = json.loads((Path(result["job_dir"]) / "manifest.json").read_text(encoding="utf-8"))

            self.assertEqual(requested, ["https://mmbiz.qpic.cn/sz_mmbiz_png/example/640"])
            self.assertEqual(manifest["media"]["failed"], [])
            self.assertEqual(len(manifest["media"]["downloaded"]), 1)

    def test_wechat_image_http_upgrade_keeps_strict_host_and_authority_boundary(self):
        unchanged = [
            "http://evil.example/image.png",
            "http://mmbiz.qpic.cn.evil.example/image.png",
            "http://user@mmbiz.qpic.cn/image.png",
            "http://@mmbiz.qpic.cn/image.png",
            "http://:@mmbiz.qpic.cn/image.png",
            "http://mmbiz.qpic.cn:/image.png",
            "http://mmbiz.qpic.cn./image.png",
            "http://mmbiz.qpic.cn:080/image.png",
            "http://mmbiz.qpic.cn:8080/image.png",
        ]
        for url in unchanged:
            with self.subTest(url=url):
                self.assertEqual(archive.normalize_official_media_url(url), url)
        self.assertEqual(
            archive.normalize_official_media_url("http://mmbiz.qpic.cn:80/image.png"),
            "https://mmbiz.qpic.cn/image.png",
        )
        self.assertEqual(
            archive.normalize_official_media_url("http://MMBIZ.QPIC.CN/image.png"),
            "https://mmbiz.qpic.cn/image.png",
        )

    def test_archive_article_accepts_titled_image_only_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = "https://mp.weixin.qq.com/s?__biz=biz-one&mid=1&idx=1&sn=test"
            html_body = """
                <html><head><meta property='og:title' content='image post'></head>
                <body><script>var prompt = '请完成验证';</script><div id='js_content'>
                <img data-src='https://mmbiz.qpic.cn/a.png'/>
                <img data-src='https://mmbiz.qpic.cn/b.png'/>
                </div></body></html>
            """.encode()

            def media_fetcher(url):
                return b"image", "image/png", url

            result = archive.archive_article_html(source, html_body, root, media_fetcher=media_fetcher)
            manifest = json.loads((Path(result["job_dir"]) / "manifest.json").read_text(encoding="utf-8"))

            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(manifest["media"]["downloaded"]), 2)

    def test_archive_article_rejects_generic_untitled_or_verification_image_pages(self):
        source = "https://mp.weixin.qq.com/s?__biz=biz-one&mid=1&idx=1&sn=test"
        pages = [
            b"<html><head><meta property='og:title' content='WeChat'></head><body><div id='js_content'><img data-src='https://mmbiz.qpic.cn/a.png'/></div></body></html>",
            b"<html><body><div id='js_content'><img data-src='https://mmbiz.qpic.cn/a.png'/></div></body></html>",
            "<html><head><meta property='og:title' content='温馨提示'></head><body><div id='js_content'>请完成验证<img data-src='http://evil.example/pixel.png'/></div></body></html>".encode(),
            "<html><head><meta property='og:title' content='温馨提示'></head><body><p>请完成验证</p><div id='js_content'><img data-src='https://mmbiz.qpic.cn/a.png'/></div></body></html>".encode(),
        ]
        for html_body in pages:
            with self.subTest(html_body=html_body), tempfile.TemporaryDirectory() as temporary:
                with self.assertRaises(archive.ArchiveError) as caught:
                    archive.archive_article_html(source, html_body, Path(temporary))
                self.assertEqual(caught.exception.code, "article_not_found")

    def test_archive_article_image_only_requires_a_downloaded_image(self):
        source = "https://mp.weixin.qq.com/s?__biz=biz-one&mid=1&idx=1&sn=test"
        cases = [
            ("http://evil.example/image.png", lambda _: self.fail("invalid media must not be fetched")),
            ("https://mmbiz.qpic.cn/empty.png", lambda url: (b"", "image/png", url)),
            ("https://mmbiz.qpic.cn/not-image.png", lambda url: (b"html", "text/html", url)),
        ]
        for media_url, media_fetcher in cases:
            with self.subTest(media_url=media_url), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                html_body = f"""
                    <html><head><meta property='og:title' content='image post'></head>
                    <body><div id='js_content'><img data-src='{media_url}'/></div></body></html>
                """.encode()

                with self.assertRaises(archive.ArchiveError) as caught:
                    archive.archive_article_html(source, html_body, root, media_fetcher=media_fetcher)

                self.assertEqual(caught.exception.code, "article_media_incomplete")
                manifests = list((root / "jobs").glob("article-*/manifest.json"))
                self.assertEqual(len(manifests), 1)
                manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
                self.assertEqual(manifest["status"], "failed")
                self.assertEqual(manifest["error"]["code"], "article_media_incomplete")

    def test_archive_article_isolates_malformed_media_ports(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = "https://mp.weixin.qq.com/s?__biz=biz-one&mid=1&idx=1&sn=test"
            html_body = b"""
                <html><head><meta property='og:title' content='text post'></head>
                <body><div id='js_content'>article body text long enough
                <img data-src='http://mmbiz.qpic.cn:notaport/image.png'/>
                </div></body></html>
            """

            result = archive.archive_article_html(source, html_body, root, media_fetcher=lambda _: self.fail("malformed media must not be fetched"))
            manifest = json.loads((Path(result["job_dir"]) / "manifest.json").read_text(encoding="utf-8"))

            self.assertEqual(result["status"], "completed")
            self.assertEqual(manifest["media"]["downloaded"], [])
            self.assertEqual([item["error"] for item in manifest["media"]["failed"]], ["invalid_url"])

    def test_submit_official_batch_reused_child_backfills_inventory_publish_time(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent_dir = root / "jobs" / "batch-20000101T000000Z-00000000"
            child_dir = root / "jobs" / "content-20000101T000000Z-11111111"
            parent_dir.mkdir(parents=True)
            child_dir.mkdir(parents=True)
            child = {
                "job_id": child_dir.name,
                "kind": "content",
                "platform": "wechat_official_account",
                "content_id": "wechat-official:biz-one:1:1:test",
                "status": "completed",
            }
            archive.write_json(child_dir / "manifest.json", child)
            parent_path = parent_dir / "manifest.json"
            manifest = {
                "job_id": parent_dir.name,
                "kind": "batch",
                "platform": "wechat_official_account",
                "status": "processing",
                "selection": {"limit": 1, "order": "newest"},
                "items": [
                    {
                        "content_id": child["content_id"],
                        "canonical_url": "https://mp.weixin.qq.com/s?__biz=biz-one&mid=1&idx=1&sn=test",
                        "title": "测试文章",
                        "published_at": "2026-08-12T12:00:00Z",
                        "child_job_id": None,
                        "result": "discovered",
                    }
                ],
            }
            archive.write_json(parent_path, manifest)
            with patch.object(archive, "refresh_official_batch", side_effect=lambda value, *_: value):
                archive.submit_official_batch_children(manifest, parent_path, root)
            updated = json.loads((child_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(updated["published_at"], "2026-08-12T12:00:00Z")
            self.assertEqual(updated["title"], "测试文章")

    def test_submit_official_batch_children_copies_inventory_publish_time(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent_dir = root / "jobs" / "batch-20260813T000000Z-aaaaaaaa"
            parent_dir.mkdir(parents=True)
            parent_path = parent_dir / "manifest.json"
            source = "https://mp.weixin.qq.com/s?__biz=biz-one&mid=1&idx=1&sn=test"
            manifest = {
                "job_id": parent_dir.name,
                "kind": "batch",
                "platform": "wechat_official_account",
                "status": "processing",
                "account": {"name": "测试公众号", "account_id": "account-one", "biz": "biz-one"},
                "selection": {"limit": 1, "order": "newest"},
                "items": [
                    {
                        "content_id": "article-one",
                        "canonical_url": source,
                        "title": "测试文章",
                        "published_at": "2026-08-12T12:00:00Z",
                        "child_job_id": None,
                        "result": "discovered",
                    }
                ],
            }
            archive.write_json(parent_path, manifest)
            with patch.object(archive, "refresh_official_batch", side_effect=lambda value, *_: value):
                updated_batch = archive.submit_official_batch_children(manifest, parent_path, root)
            child_id = updated_batch["items"][0]["child_job_id"]
            child = json.loads((root / "jobs" / child_id / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(child["published_at"], "2026-08-12T12:00:00Z")
            self.assertEqual(child["title"], "测试文章")
            self.assertEqual(child["account"], manifest["account"])


if __name__ == "__main__":
    unittest.main()
