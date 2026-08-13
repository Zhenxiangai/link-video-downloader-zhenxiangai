import importlib.util
import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "wechat_archive.py"
SPEC = importlib.util.spec_from_file_location("wechat_archive_registry", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load wechat_archive module")
archive = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(archive)


def register_many(root_text: str, prefix: str) -> None:
    root = Path(root_text)
    for index in range(20):
        username = f"{prefix}-{index}@finder"
        archive.register_channel_creator(
            root,
            f"https://weixin.qq.com/sph/{prefix}-{index}",
            {"username": username, "nickname": f"{prefix}-{index}"},
        )


class ChannelsRegistryConcurrencyTests(unittest.TestCase):
    def test_concurrent_writers_preserve_all_creator_mappings(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = multiprocessing.Process(target=register_many, args=(temporary, "a"))
            second = multiprocessing.Process(target=register_many, args=(temporary, "b"))
            first.start()
            second.start()
            first.join(10)
            second.join(10)
            self.assertEqual(first.exitcode, 0)
            self.assertEqual(second.exitcode, 0)
            registry = json.loads((Path(temporary) / "state" / "channels-creators.json").read_text())
            self.assertEqual(len(registry["creators"]), 40)
            self.assertEqual(len(registry["sources"]), 40)


if __name__ == "__main__":
    unittest.main()
