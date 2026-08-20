import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_ROOT = REPOSITORY_ROOT / "skill-releases" / "v1.2.4" / "wechat-archive"
EXPECTED_FILES = (
    "SKILL.md",
    "references/LICENSE.md",
    "references/THIRD_PARTY_NOTICES.md",
    "scripts/bootstrap.sh",
    "scripts/manage_transcriber.sh",
    "scripts/wechat_archive.py",
)


class VersionedSkillBundleTests(unittest.TestCase):
    def test_current_stable_bundle_matches_the_reviewed_root_files(self):
        for relative in EXPECTED_FILES:
            with self.subTest(relative=relative):
                bundled = BUNDLE_ROOT / relative
                source = REPOSITORY_ROOT / relative
                self.assertTrue(bundled.is_file())
                self.assertFalse(bundled.is_symlink())
                self.assertEqual(bundled.read_bytes(), source.read_bytes())

    def test_stable_manifest_and_worker_versions_match(self):
        expected = "1.2.4"
        skill = (REPOSITORY_ROOT / "SKILL.md").read_text(encoding="utf-8")
        worker = (REPOSITORY_ROOT / "scripts/wechat_archive.py").read_text(encoding="utf-8")
        skill_versions = [line.strip() for line in skill.splitlines() if line.startswith("version:")]
        self.assertEqual(skill_versions, [f"version: {expected}"])
        worker_versions = [line.strip() for line in worker.splitlines() if line.startswith("VERSION =")]
        self.assertEqual(worker_versions, [f'VERSION = "{expected}"'])



if __name__ == "__main__":
    unittest.main()
