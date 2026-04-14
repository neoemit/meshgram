import unittest

from meshgram.reply_links import ReplyLinkRegistry


class _DeterministicReplyLinkRegistry(ReplyLinkRegistry):
    def __init__(self, ttl_hours: int = 24):
        super().__init__(ttl_hours=ttl_hours)
        self.now = 1_000.0

    def _now(self) -> float:
        return self.now


class ReplyLinkRegistryTests(unittest.TestCase):
    def test_bidirectional_lookup(self):
        registry = _DeterministicReplyLinkRegistry(ttl_hours=24)
        registry.link_telegram_to_meshtastic(-100, 10, 501)
        registry.link_meshtastic_to_telegram(501, -100, 10)

        self.assertEqual(registry.get_meshtastic_for_telegram(-100, 10), 501)
        self.assertEqual(registry.get_telegram_for_meshtastic(-100, 501), 10)

    def test_expiration(self):
        registry = _DeterministicReplyLinkRegistry(ttl_hours=1)
        registry.link_telegram_to_meshtastic(-100, 10, 501)
        registry.link_meshtastic_to_telegram(501, -100, 10)

        registry.now += (60 * 60) + 1

        self.assertIsNone(registry.get_meshtastic_for_telegram(-100, 10))
        self.assertIsNone(registry.get_telegram_for_meshtastic(-100, 501))

    def test_overwrite(self):
        registry = _DeterministicReplyLinkRegistry(ttl_hours=24)
        registry.link_telegram_to_meshtastic(-100, 10, 501)
        registry.link_telegram_to_meshtastic(-100, 10, 777)

        self.assertEqual(registry.get_meshtastic_for_telegram(-100, 10), 777)

    def test_chat_scope_guard(self):
        registry = _DeterministicReplyLinkRegistry(ttl_hours=24)
        registry.link_meshtastic_to_telegram(501, -100, 10)

        self.assertEqual(registry.get_telegram_for_meshtastic(-100, 501), 10)
        self.assertIsNone(registry.get_telegram_for_meshtastic(-200, 501))

    def test_chunk_style_mapping(self):
        registry = _DeterministicReplyLinkRegistry(ttl_hours=24)
        registry.link_telegram_to_meshtastic(-100, 44, 1001)  # canonical first chunk
        registry.link_meshtastic_to_telegram(1001, -100, 44)
        registry.link_meshtastic_to_telegram(1002, -100, 44)  # additional chunk
        registry.link_meshtastic_to_telegram(1003, -100, 44)  # additional chunk

        self.assertEqual(registry.get_meshtastic_for_telegram(-100, 44), 1001)
        self.assertEqual(registry.get_telegram_for_meshtastic(-100, 1001), 44)
        self.assertEqual(registry.get_telegram_for_meshtastic(-100, 1002), 44)
        self.assertEqual(registry.get_telegram_for_meshtastic(-100, 1003), 44)


if __name__ == "__main__":
    unittest.main()
