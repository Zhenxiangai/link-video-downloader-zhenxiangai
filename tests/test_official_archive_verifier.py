import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE = REPOSITORY_ROOT / "scripts" / "verify_official_archive.py"
SPEC = importlib.util.spec_from_file_location("verify_official_archive_test", SOURCE)
assert SPEC is not None and SPEC.loader is not None
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def output_record(root: Path, path: Path, role: str) -> dict:
    data = path.read_bytes()
    return {
        "role": role,
        "path": path.relative_to(root).as_posix(),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


class OfficialArchiveVerifierTests(unittest.TestCase):
    def create_archive(self, root: Path) -> tuple[str, Path]:
        parent_job_id = "batch-20000101T000000Z-00000000"
        completed_job_id = "content-20000101T000000Z-11111111"
        unavailable_job_id = "content-20000101T000000Z-22222222"
        output_dir = root / "content" / "公众号" / "article--one"
        output_dir.mkdir(parents=True)
        original = output_dir / "original.html"
        body = output_dir / "正文.md"
        original.write_bytes(b"<html><body><div id='js_content'>body long enough for article</div></body></html>")
        body.write_text("# article\n\nbody", encoding="utf-8")

        completed = {
            "job_id": completed_job_id,
            "parent_job_id": parent_job_id,
            "status": "completed",
            "content_id": "content-one",
            "canonical_url": "https://mp.weixin.qq.com/s?__biz=test&mid=1&idx=1&sn=one",
            "output_dir": output_dir.relative_to(root).as_posix(),
            "outputs": [
                output_record(root, original, "original_html"),
                output_record(root, body, "body_markdown"),
            ],
        }
        unavailable_dir = root / "jobs" / unavailable_job_id
        unavailable = {
            "job_id": unavailable_job_id,
            "parent_job_id": parent_job_id,
            "status": "unavailable",
            "content_id": "content-two",
            "canonical_url": "https://mp.weixin.qq.com/s?__biz=test&mid=2&idx=1&sn=two",
            "outputs": [],
        }
        write_json(root / "jobs" / completed_job_id / "manifest.json", completed)
        write_json(unavailable_dir / "manifest.json", unavailable)
        (unavailable_dir / "original.html").write_text("<html><body></body></html>", encoding="utf-8")

        parent = {
            "job_id": parent_job_id,
            "status": "completed",
            "items": [
                {
                    "content_id": completed["content_id"],
                    "canonical_url": completed["canonical_url"],
                    "child_job_id": completed_job_id,
                },
                {
                    "content_id": unavailable["content_id"],
                    "canonical_url": unavailable["canonical_url"],
                    "child_job_id": unavailable_job_id,
                },
            ],
        }
        write_json(root / "jobs" / parent_job_id / "manifest.json", parent)
        return parent_job_id, body

    def test_verified_summary_is_anonymized_and_deterministic(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent_job_id, _ = self.create_archive(root)

            first = verifier.verify_archive(root, parent_job_id)
            second = verifier.verify_archive(root, parent_job_id)

            self.assertEqual(first, second)
            self.assertEqual(first["hard_errors"], 0)
            self.assertEqual(first["parent_items"], 2)
            self.assertEqual(first["status_counts"], {"completed": 1, "unavailable": 1})
            self.assertEqual(first["verified_output_files"], 2)
            self.assertEqual(first["unavailable_secondary_review"]["empty_title"], 1)
            self.assertEqual(first["unavailable_secondary_review"]["empty_body"], 1)
            self.assertEqual(first["unavailable_secondary_review"]["empty_images"], 1)
            self.assertEqual(len(first["inventory_root_sha256"]), 64)
            self.assertEqual(len(first["output_record_root_sha256"]), 64)
            self.assertEqual(len(first["snapshot_sha256"]), 64)
            serialized = json.dumps(first, ensure_ascii=False)
            self.assertNotIn("article--one", serialized)
            self.assertNotIn("mp.weixin.qq.com", serialized)

    def test_tampered_output_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent_job_id, body = self.create_archive(root)
            body.write_text("tampered", encoding="utf-8")

            summary = verifier.verify_archive(root, parent_job_id)

            self.assertGreater(summary["hard_errors"], 0)
            self.assertEqual(summary["checksum_failures"], 1)
            self.assertEqual(summary["error_counts"]["checksum_mismatch"], 1)
            self.assertEqual(summary["error_counts"]["byte_mismatch"], 1)


if __name__ == "__main__":
    unittest.main()
