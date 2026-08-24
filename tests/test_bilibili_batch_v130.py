import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "wechat_archive.py"
SPEC = importlib.util.spec_from_file_location("wechat_archive_bilibili_v130", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load wechat_archive module")
archive = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(archive)


BVIDS = ("BV1abcdefghi", "BV1bcdefghij", "BV1cdefghijk")


def creator_batch(root: Path, collections=None):
    job_id, job_dir, manifest = archive.new_job(root, "batch", f"https://www.bilibili.com/video/{BVIDS[0]}/")
    manifest.update(
        {
            "kind": "creator_batch",
            "platform": "bilibili",
            "status": "awaiting_download_count",
            "inventory": {"items": [{"id": bvid, "url": f"https://www.bilibili.com/video/{bvid}/"} for bvid in BVIDS]},
            "collections": collections or [],
            "selection": None,
            "child_job_ids": [],
            "counts": {"selected": 0, "processing": 0, "completed": 0, "failed": 0},
        }
    )
    manifest_path = job_dir / "manifest.json"
    archive.write_json(manifest_path, manifest)
    return job_id, manifest_path, manifest


class BilibiliBatchV130Tests(unittest.TestCase):
    def test_bilibili_api_uses_space_referer_without_relaxing_host_limit(self):
        payload = json.dumps({"code": 0, "data": {"items_lists": {}}}).encode()
        with patch.object(archive, "fetch_limited", return_value=(payload, "application/json", "https://api.bilibili.com/test")) as fetch:
            archive.bilibili_json("/test", {"mid": "123"})
        fetch.assert_called_once_with(
            "https://api.bilibili.com/test?mid=123",
            exact_hosts={"api.bilibili.com"},
            max_bytes=4 * 1024 * 1024,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://space.bilibili.com/123/"},
        )

    def test_same_bvid_reuses_one_content_job_despite_tracking_query(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = archive.submit_content(f"https://www.bilibili.com/video/{BVIDS[0]}/?vd_source=one", root)
            second = archive.submit_content(f"https://www.bilibili.com/video/{BVIDS[0]}/?vd_source=two", root)
            self.assertFalse(first["reused"])
            self.assertTrue(second["reused"])
            self.assertEqual(first["job_id"], second["job_id"])
            self.assertEqual(len(list((root / "jobs").glob("content-*/manifest.json"))), 1)

    def test_different_bilibili_parts_do_not_reuse_one_content_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = archive.submit_content(f"https://www.bilibili.com/video/{BVIDS[0]}/?p=1", root)
            second = archive.submit_content(f"https://www.bilibili.com/video/{BVIDS[0]}/?p=2", root)
            self.assertFalse(second["reused"])
            self.assertNotEqual(first["job_id"], second["job_id"])

    def test_failed_content_job_does_not_block_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = archive.submit_content(f"https://www.bilibili.com/video/{BVIDS[0]}/", root)
            manifest_path = root / first["manifest"]
            manifest = json.loads(manifest_path.read_text())
            manifest["status"] = "failed"
            archive.write_json(manifest_path, manifest)
            retry = archive.submit_content(f"https://www.bilibili.com/video/{BVIDS[0]}/", root)
            self.assertFalse(retry["reused"])
            self.assertNotEqual(first["job_id"], retry["job_id"])

    def test_legacy_completed_manifest_is_reused_by_bvid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job_id, job_dir, manifest = archive.new_job(root, "content", f"https://www.bilibili.com/video/{BVIDS[0]}/")
            manifest.update({"platform": "bilibili", "status": "completed", "content_id": BVIDS[0]})
            archive.write_json(job_dir / "manifest.json", manifest)
            result = archive.submit_content(f"https://www.bilibili.com/video/{BVIDS[0]}/?share_source=copy_web", root)
            self.assertTrue(result["reused"])
            self.assertEqual(result["job_id"], job_id)

    def test_collection_inventory_freezes_all_pages_and_removes_duplicates(self):
        responses = [
            {
                "items_lists": {
                    "page": {"total": 1},
                    "seasons_list": [{"meta": {"season_id": 7, "name": "送礼", "total": 3}}],
                }
            },
            {"page": {"total": 3}, "archives": [{"bvid": BVIDS[0]}, {"bvid": BVIDS[1]}]},
            {"page": {"total": 3}, "archives": [{"bvid": BVIDS[1]}, {"bvid": BVIDS[2]}]},
        ]
        with patch.object(archive, "bilibili_json", side_effect=responses):
            result = archive.bilibili_creator_collections("123")
        self.assertEqual(result, [{"id": "7", "name": "送礼", "available": 3, "item_ids": list(BVIDS)}])

    def test_collection_inventory_removes_duplicate_seasons_across_pages(self):
        season = {"meta": {"season_id": 7, "name": "送礼", "total": 1}}
        responses = [
            {"items_lists": {"page": {"total": 2}, "seasons_list": [season]}},
            {"page": {"total": 1}, "archives": [{"bvid": BVIDS[0]}]},
            {"items_lists": {"page": {"total": 2}, "seasons_list": [season]}},
        ]
        with patch.object(archive, "bilibili_json", side_effect=responses):
            result = archive.bilibili_creator_collections("123")
        self.assertEqual(result, [{"id": "7", "name": "送礼", "available": 1, "item_ids": [BVIDS[0]]}])

    def test_collection_api_failure_preserves_legacy_creator_inventory(self):
        class FakeYoutubeDL:
            def __init__(self, _options):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def extract_info(self, _url, download=False):
                self.assert_download = download
                return {"entries": [{"id": BVIDS[0], "title": "one", "timestamp": 1}]}

        yt_dlp = types.ModuleType("yt_dlp")
        yt_dlp.YoutubeDL = FakeYoutubeDL
        yt_dlp_utils = types.ModuleType("yt_dlp.utils")
        yt_dlp_utils.DownloadError = RuntimeError
        view = {"owner": {"mid": 123, "name": "creator"}}
        unavailable = archive.ArchiveError("bilibili_api_failed", "temporary", 69)
        with (
            patch.dict(sys.modules, {"yt_dlp": yt_dlp, "yt_dlp.utils": yt_dlp_utils}),
            patch.object(archive, "resolve_transparent_core_url", return_value=f"https://www.bilibili.com/video/{BVIDS[0]}/"),
            patch.object(archive, "transparent_core_root", return_value=Path("/tmp/transparent-core-test")),
            patch.object(archive, "bilibili_json", return_value=view),
            patch.object(archive, "bilibili_creator_collections", side_effect=unavailable),
        ):
            creator, items, collections = archive.bilibili_creator_inventory(f"https://www.bilibili.com/video/{BVIDS[0]}/")
        self.assertEqual([item["id"] for item in items], [BVIDS[0]])
        self.assertEqual(collections, [])
        self.assertEqual(creator["collections_status"], "unavailable")

    def test_missing_selection_excludes_existing_completed_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, content_dir, content = archive.new_job(root, "content", f"https://www.bilibili.com/video/{BVIDS[0]}/")
            content.update({"platform": "bilibili", "status": "completed", "content_id": BVIDS[0]})
            archive.write_json(content_dir / "manifest.json", content)
            batch_id, _, _ = creator_batch(root)

            def submit(selected, manifest_path, _root):
                selected["status"] = "processing"
                selected["counts"] = {"selected": 2, "processing": 2, "completed": 0, "failed": 0}
                archive.write_json(manifest_path, selected)
                return selected

            with patch.object(archive, "_submit_creator_batch_children_unlocked", side_effect=submit):
                result = archive.download_creator_selection(batch_id, "missing", root)
            saved = json.loads((root / result["manifest"]).read_text())
            self.assertEqual(saved["selection"]["ids"], [BVIDS[1], BVIDS[2]])
            self.assertEqual(result["selection"], {"mode": "missing", "selected": 2})

    def test_collection_selection_uses_frozen_membership(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            collection = {"id": "7", "name": "送礼", "available": 2, "item_ids": [BVIDS[0], BVIDS[2]]}
            batch_id, _, _ = creator_batch(root, [collection])

            def submit(selected, manifest_path, _root):
                selected["status"] = "processing"
                selected["counts"] = {"selected": 2, "processing": 2, "completed": 0, "failed": 0}
                archive.write_json(manifest_path, selected)
                return selected

            with patch.object(archive, "_submit_creator_batch_children_unlocked", side_effect=submit):
                result = archive.download_creator_selection(batch_id, "collection", root, collection_id="7")
            saved = json.loads((root / result["manifest"]).read_text())
            self.assertEqual(saved["selection"]["ids"], [BVIDS[0], BVIDS[2]])
            self.assertEqual(result["selection"]["collection_name"], "送礼")

    def test_explicit_selection_rejects_bvid_outside_frozen_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            batch_id, _, _ = creator_batch(root)
            with self.assertRaisesRegex(archive.ArchiveError, "不属于该博主"):
                archive.download_creator_selection(batch_id, "explicit", root, content_ids=["BV1zzzzzzzzz"])

    def test_explicit_selection_rejects_invalid_or_duplicate_bvids(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for content_ids in (["not-a-bvid"], [BVIDS[0], BVIDS[0]]):
                batch_id, _, _ = creator_batch(root)
                with self.subTest(content_ids=content_ids), self.assertRaises(archive.ArchiveError):
                    archive.download_creator_selection(batch_id, "explicit", root, content_ids=content_ids)

    def test_legacy_numeric_count_and_compact_status_remain_compatible(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            collection = {"id": "7", "name": "送礼", "available": 2, "item_ids": [BVIDS[0], BVIDS[1]]}
            batch_id, manifest_path, manifest = creator_batch(root, [collection])

            def submit(selected, selected_path, _root):
                selected["status"] = "processing"
                selected["counts"] = {"selected": 2, "processing": 2, "completed": 0, "failed": 0}
                selected["child_job_ids"] = ["content-20260825T000000Z-00000001", "content-20260825T000000Z-00000002"]
                archive.write_json(selected_path, selected)
                return selected

            with patch.object(archive, "_submit_creator_batch_children_unlocked", side_effect=submit):
                planned = archive.download_creator_plan(batch_id, 2, root)
            self.assertEqual(planned["counts"]["selected"], 2)

            manifest = json.loads(manifest_path.read_text())
            manifest["coverage"] = {"classified": 2, "unclassified": 1}
            archive.write_json(manifest_path, manifest)
            with patch.object(archive, "refresh_creator_batch", return_value=manifest):
                status = archive.job_status(batch_id, root)["job"]
            self.assertEqual(status["selection"], {"mode": "count", "selected": 2, "limit": 2, "order": "newest"})
            self.assertEqual(status["collections"], [{"id": "7", "name": "送礼", "available": 2}])
            self.assertNotIn("ids", status["selection"])
            self.assertNotIn("item_ids", status["collections"][0])

    def test_empty_missing_selection_completes_without_children(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for bvid in BVIDS:
                _, job_dir, content = archive.new_job(root, "content", f"https://www.bilibili.com/video/{bvid}/")
                content.update({"platform": "bilibili", "status": "completed", "content_id": bvid})
                archive.write_json(job_dir / "manifest.json", content)
            batch_id, _, _ = creator_batch(root)
            result = archive.download_creator_selection(batch_id, "missing", root)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["selection"], {"mode": "missing", "selected": 0})
            self.assertEqual(result["child_job_ids"], [])


if __name__ == "__main__":
    unittest.main()
