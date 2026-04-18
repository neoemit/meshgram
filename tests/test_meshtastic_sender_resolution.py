import unittest

from meshgram.app import MeshgramApp, MeshtasticClient
from meshgram.config import MeshgramSettings
from meshgram.types import SendMeshtasticAction


class _FakeInterface:
    def __init__(self, nodes):
        self.nodes = nodes


class _SendTextWithReplyId:
    def __init__(self):
        self.calls = []

    def sendText(self, text, destinationId=None, channelIndex=0, replyId=None, wantAck=False):
        self.calls.append(
            {
                "text": text,
                "destinationId": destinationId,
                "channelIndex": channelIndex,
                "replyId": replyId,
                "wantAck": wantAck,
            }
        )
        return {"id": 123}


class _SendTextWithoutReplyId:
    def __init__(self):
        self.calls = []

    def sendText(self, text, destinationId=None, channelIndex=0, wantAck=False):
        self.calls.append(
            {
                "text": text,
                "destinationId": destinationId,
                "channelIndex": channelIndex,
                "wantAck": wantAck,
            }
        )
        return {"id": 321}


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

    def test_build_event_parses_string_packet_id(self):
        app = MeshgramApp(self._settings())
        packet = {
            "fromId": "!1234abcd",
            "channel": 1,
            "id": "123456",
            "decoded": {
                "portnum": "TEXT_MESSAGE_APP",
                "payload": b"hello",
            },
        }

        event = app._build_meshtastic_event(packet)
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.packet_id, 123456)

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

    def test_send_text_uses_reply_id_when_interface_supports_it(self):
        client = MeshtasticClient(self._settings())
        iface = _SendTextWithReplyId()
        client.iface = iface

        result = client.send_text(
            SendMeshtasticAction(
                text="pong",
                channel_index=2,
                reply_id=999,
                want_ack=True,
            )
        )

        self.assertEqual(result["id"], 123)
        self.assertEqual(len(iface.calls), 1)
        self.assertEqual(iface.calls[0]["replyId"], 999)

    def test_send_text_falls_back_when_reply_id_is_unsupported(self):
        client = MeshtasticClient(self._settings())
        iface = _SendTextWithoutReplyId()
        client.iface = iface

        first_result = client.send_text(
            SendMeshtasticAction(
                text="first",
                channel_index=1,
                reply_id=111,
            )
        )
        second_result = client.send_text(
            SendMeshtasticAction(
                text="second",
                channel_index=1,
                reply_id=222,
            )
        )

        self.assertEqual(first_result["id"], 321)
        self.assertEqual(second_result["id"], 321)
        self.assertEqual(client._supports_sendtext_reply_id, False)
        self.assertEqual(len(iface.calls), 2)
        self.assertEqual(iface.calls[0]["text"], "first")
        self.assertEqual(iface.calls[1]["text"], "second")


if __name__ == "__main__":
    unittest.main()
