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
                "status": "processing",
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


if __name__ == "__main__":
    unittest.main()
