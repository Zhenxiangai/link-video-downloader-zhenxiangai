import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "wechat_archive.py"
SPEC = importlib.util.spec_from_file_location("wechat_archive_official_fairness", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load wechat_archive module")
archive = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(archive)


class OfficialBatchWorkerFairnessTests(unittest.TestCase):
    def test_actionable_batch_precedes_older_waiting_batch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jobs = root / "jobs"
            jobs.mkdir(parents=True)
            waiting_id = "batch-20260801T000000Z-aaaaaaaa"
            ready_id = "batch-20260813T000000Z-bbbbbbbb"
            for job_id, status in ((waiting_id, "waiting_for_authorization"), (ready_id, "queued")):
                job_dir = jobs / job_id
                job_dir.mkdir()
                (job_dir / "manifest.json").write_text(
                    json.dumps({"kind": "batch", "status": status}),
                    encoding="utf-8",
                )

            processed = []
            with (
                patch.object(archive, "resume_waiting_channel_creators_once", return_value=0),
                patch.object(archive, "progress_channel_jobs_once", return_value=0),
                patch.object(archive, "process_official_batch", side_effect=lambda path, root: processed.append(path.parent.name)),
            ):
                archive.content_worker_once(root)

            self.assertEqual(processed, [ready_id])


if __name__ == "__main__":
    unittest.main()
