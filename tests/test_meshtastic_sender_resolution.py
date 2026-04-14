import unittest

from meshgram.app import MeshgramApp, MeshtasticClient
from meshgram.config import MeshgramSettings


class _FakeInterface:
    def __init__(self, nodes):
        self.nodes = nodes


class MeshtasticSenderResolutionTests(unittest.TestCase):
    def _settings(self) -> MeshgramSettings:
        return MeshgramSettings(
            telegram_bot_token="token",
            telegram_group_id=-100,
            config_path="config.yaml",
            plugins=[],
        )

    def test_build_event_uses_numeric_sender_when_from_id_missing(self):
        app = MeshgramApp(self._settings())
        app.meshtastic.resolve_sender_label = lambda from_id, from_num=None: f"{from_id}:{from_num}"
        packet = {
            "from": 0x1234ABCD,
            "channel": 1,
            "id": 10,
            "decoded": {
                "portnum": "TEXT_MESSAGE_APP",
                "payload": b"hello",
            },
        }

        event = app._build_meshtastic_event(packet)
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.from_id, "!1234abcd")
        self.assertEqual(event.sender_label, "!1234abcd:305441741")

    def test_sender_label_uses_override_without_node_db(self):
        settings = self._settings()
        settings.meshtastic.node_name_overrides = {"!1234abcd": "FieldNode"}
        client = MeshtasticClient(settings)

        self.assertEqual(client.resolve_sender_label("!1234ABCD"), "FieldNode")
        self.assertEqual(client.resolve_sender_label(None, from_num=0x1234ABCD), "FieldNode")

    def test_sender_label_uses_short_name_from_node_db(self):
        client = MeshtasticClient(self._settings())
        client.iface = _FakeInterface(
            {
                0x1234ABCD: {
                    "num": 0x1234ABCD,
                    "user": {
                        "id": "!1234abcd",
                        "shortName": "RPT",
                        "longName": "Repeater",
                    },
                }
            }
        )

        self.assertEqual(client.resolve_sender_label(None, from_num=0x1234ABCD), "RPT")

    def test_sender_label_falls_back_to_normalized_node_id(self):
        client = MeshtasticClient(self._settings())
        self.assertEqual(client.resolve_sender_label(None, from_num=0x42), "!00000042")


if __name__ == "__main__":
    unittest.main()
