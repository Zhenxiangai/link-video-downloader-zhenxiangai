import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_ROOT = REPOSITORY_ROOT / "skill-releases" / "v1.2.3" / "wechat-archive"
EXPECTED_FILES = (
    "SKILL.md",
    "references/LICENSE.md",
    "references/THIRD_PARTY_NOTICES.md",
    "scripts/bootstrap.sh",
    "scripts/manage_transcriber.sh",
    "scripts/wechat_archive.py",
)


class VersionedSkillBundleTests(unittest.TestCase):
    def test_v123_bundle_links_to_the_reviewed_root_files(self):
        for relative in EXPECTED_FILES:
            with self.subTest(relative=relative):
                bundled = BUNDLE_ROOT / relative
                source = REPOSITORY_ROOT / relative
                self.assertTrue(bundled.is_symlink())
                self.assertEqual(bundled.resolve(), source.resolve())
                self.assertEqual(bundled.read_bytes(), source.read_bytes())


if __name__ == "__main__":
    unittest.main()
