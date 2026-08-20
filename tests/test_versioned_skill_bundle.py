import hashlib
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_ROOT = REPOSITORY_ROOT / "skill-releases" / "v1.2.5" / "wechat-archive"
EXPECTED_FILES = (
    "SKILL.md",
    "references/LICENSE.md",
    "references/THIRD_PARTY_NOTICES.md",
    "scripts/bootstrap.sh",
    "scripts/manage_transcriber.sh",
    "scripts/wechat_archive.py",
)
RELEASED_BUNDLE_SHA256 = {
    "v1.2.4": {
        "SKILL.md": "3d89ac1be5c8952c04375422e72514ab8befd77196cb9c44cfdb1b2e466af3e5",
        "references/LICENSE.md": "3e285b32b2a9f98662fb6f0f603ac345e51d2b8ad43feb6334e290816d719755",
        "references/THIRD_PARTY_NOTICES.md": "6c87795e74d9e65b66b8adb7d94f7322abff4d06e1fe5fd522300cee9101bee2",
        "scripts/bootstrap.sh": "2250b587828669fbddfc07b16d0d8c133ab2d279af6603f41df8758ac8f70393",
        "scripts/manage_transcriber.sh": "3c89e64e6ec318a6830cbf758774a897b36133718a86d35bb02f58691c82daa1",
        "scripts/wechat_archive.py": "5adb17f23f832f5ca97c3ca7e03ae820d81cba65f6b3da4f9f46ce44a1f6fa4d",
    }
}


class VersionedSkillBundleTests(unittest.TestCase):
    def test_released_historical_bundles_are_immutable(self):
        for version, expected_files in RELEASED_BUNDLE_SHA256.items():
            for relative, expected_sha256 in expected_files.items():
                with self.subTest(version=version, relative=relative):
                    path = REPOSITORY_ROOT / "skill-releases" / version / "wechat-archive" / relative
                    self.assertTrue(path.is_file())
                    self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected_sha256)

    def test_current_stable_bundle_matches_the_reviewed_root_files(self):
        for relative in EXPECTED_FILES:
            with self.subTest(relative=relative):
                bundled = BUNDLE_ROOT / relative
                source = REPOSITORY_ROOT / relative
                self.assertTrue(bundled.is_file())
                self.assertFalse(bundled.is_symlink())
                self.assertEqual(bundled.read_bytes(), source.read_bytes())

    def test_stable_manifest_and_worker_versions_match(self):
        expected = "1.2.5"
        skill = (REPOSITORY_ROOT / "SKILL.md").read_text(encoding="utf-8")
        worker = (REPOSITORY_ROOT / "scripts/wechat_archive.py").read_text(encoding="utf-8")
        skill_versions = [line.strip() for line in skill.splitlines() if line.startswith("version:")]
        self.assertEqual(skill_versions, [f"version: {expected}"])
        worker_versions = [line.strip() for line in worker.splitlines() if line.startswith("VERSION =")]
        self.assertEqual(worker_versions, [f'VERSION = "{expected}"'])



if __name__ == "__main__":
    unittest.main()
