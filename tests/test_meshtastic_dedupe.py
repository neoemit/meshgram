import unittest
from unittest.mock import patch

from meshgram.app import MeshgramApp
from meshgram.config import MeshgramSettings


class MeshtasticDedupeTests(unittest.TestCase):
    def _app(self) -> MeshgramApp:
        settings = MeshgramSettings(
            telegram_bot_token="token",
            telegram_group_id=-100,
            config_path="config.yaml",
            plugins=[],
        )
        return MeshgramApp(settings)

    def test_packet_id_dedupes_within_ttl(self):
        app = self._app()

        with patch("meshgram.app.time.monotonic", side_effect=[100.0, 101.0]):
            self.assertFalse(app._is_duplicate_meshtastic_packet_id(12345))
            self.assertTrue(app._is_duplicate_meshtastic_packet_id(12345))

    def test_packet_id_expires_after_ttl(self):
        app = self._app()

        with patch("meshgram.app.time.monotonic", side_effect=[100.0, 260.0]):
            self.assertFalse(app._is_duplicate_meshtastic_packet_id(12345))
            self.assertFalse(app._is_duplicate_meshtastic_packet_id(12345))


if __name__ == "__main__":
    unittest.main()
