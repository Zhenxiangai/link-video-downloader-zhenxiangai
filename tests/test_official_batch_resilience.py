import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "wechat_archive.py"
SPEC = importlib.util.spec_from_file_location("wechat_archive_official", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load wechat_archive module")
archive = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(archive)


class OfficialBatchResilienceTests(unittest.TestCase):
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
                    "items": [{"content_id": "article-1", "child_job_id": child_id, "result": "processing"}],
                }
            )
            archive.write_json(job_dir / "manifest.json", manifest)

            refreshed = archive.refresh_official_batch(manifest, job_dir / "manifest.json", root)

            self.assertEqual(refreshed["items"][0]["result"], "failed")
            self.assertEqual(refreshed["items"][0]["error_code"], "child_manifest_invalid")
            self.assertEqual(refreshed["status"], "completed_with_failures")


if __name__ == "__main__":
    unittest.main()
