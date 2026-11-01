import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from meshgram.telegram_texts import TelegramTextCache


class TelegramTextCacheTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "telegram_messages.json"

    def test_keeps_the_latest_texts_across_instances(self):
        cache = TelegramTextCache(self.path, size=2)
        cache.remember(-1, 1, "one")
        cache.remember(-1, 2, "two")
        cache.remember(-1, 1, "one again")  # Seen again: newest now.
        cache.remember(-1, 3, "three")
        self.assertIsNone(cache.get(-1, 2))
        cache.save()
        self.assertFalse(cache.dirty)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

        loaded = TelegramTextCache(self.path, size=2)
        self.assertEqual(loaded.get(-1, 1), "one again")
        self.assertEqual(loaded.get(-1, 3), "three")
        self.assertEqual(len(loaded), 2)
        # A smaller size keeps the newest.
        self.assertEqual(len(TelegramTextCache(self.path, size=1)), 1)
        self.assertEqual(TelegramTextCache(self.path, size=1).get(-1, 3), "three")

    def test_save_only_writes_changes(self):
        cache = TelegramTextCache(self.path, size=5)
        cache.save()
        self.assertFalse(self.path.exists())

    def test_unreadable_file_is_ignored(self):
        for content in ("not json", json.dumps([1, 2])):
            self.path.write_text(content, encoding="utf-8")
            with self.assertLogs("meshgram.telegram_texts", level="WARNING"):
                self.assertEqual(len(TelegramTextCache(self.path, size=5)), 0)
        # Malformed entries are skipped, the rest kept.
        self.path.write_text(json.dumps({"messages": [[1, 2], ["a", 2, "x"], [1, 2, "ok"]]}), encoding="utf-8")
        cache = TelegramTextCache(self.path, size=5)
        self.assertEqual(len(cache), 1)
        self.assertEqual(cache.get(1, 2), "ok")

    def test_without_a_path_nothing_is_written(self):
        cache = TelegramTextCache(None, size=5)
        cache.remember(-1, 1, "hi")
        cache.save()
        self.assertEqual(cache.get(-1, 1), "hi")


if __name__ == "__main__":
    unittest.main()
