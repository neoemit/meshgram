import unittest

from meshgram.app import MeshgramApp, MeshtasticClient
from meshgram.config import MeshgramSettings
from meshgram.types import SendMeshtasticAction, SendMeshtasticReactionAction


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


class _SendTextWithoutReplyIdWithSendDataReplyId:
    def __init__(self):
        self.send_data_calls = []

    def sendText(self, text, destinationId=None, channelIndex=0, wantAck=False):
        return {"id": 201}

    def sendData(
        self,
        data,
        destinationId=None,
        portNum=None,
        wantAck=False,
        channelIndex=0,
        replyId=None,
    ):
        self.send_data_calls.append(
            {
                "data": data,
                "destinationId": destinationId,
                "portNum": portNum,
                "wantAck": wantAck,
                "channelIndex": channelIndex,
                "replyId": replyId,
            }
        )
        return {"id": 202}


class _SendTextAndSendDataWithoutReplyIdWithLowLevelPacket:
    def __init__(self):
        self.send_packet_calls = []

    def sendText(self, text, destinationId=None, channelIndex=0, wantAck=False):
        return {"id": 301}

    def sendData(self, data, destinationId=None, portNum=None, wantAck=False, channelIndex=0):
        return {"id": 302}

    def _sendPacket(self, meshPacket, destinationId="^all", wantAck=False, **kwargs):
        meshPacket.id = 4242
        self.send_packet_calls.append(
            {
                "packet": meshPacket,
                "destinationId": destinationId,
                "wantAck": wantAck,
            }
        )
        return meshPacket


class _SendTextForReactionFallback:
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
        return {"id": 777}


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

    def test_build_reaction_event_parses_emoji_and_reply_target(self):
        app = MeshgramApp(self._settings())
        packet = {
            "fromId": "!1234abcd",
            "channel": 1,
            "id": 789,
            "decoded": {
                "portnum": "TEXT_MESSAGE_APP",
                "payload": "❤".encode("utf-8"),
                "emoji": ord("❤"),
                "replyId": 456,
            },
        }

        reaction_event = app._build_meshtastic_reaction_event(packet)
        self.assertIsNotNone(reaction_event)
        assert reaction_event is not None
        self.assertEqual(reaction_event.packet_id, 789)
        self.assertEqual(reaction_event.target_packet_id, 456)
        self.assertEqual(reaction_event.emoji, "❤")

    def test_build_reaction_event_parses_string_emoji_and_numeric_portnum(self):
        app = MeshgramApp(self._settings())
        packet = {
            "fromId": "!1234abcd",
            "channel": 1,
            "id": 790,
            "decoded": {
                "portnum": 1,
                "payload": b"",
                "emoji": "🔥",
                "reply_id": "456",
            },
        }

        reaction_event = app._build_meshtastic_reaction_event(packet)
        self.assertIsNotNone(reaction_event)
        assert reaction_event is not None
        self.assertEqual(reaction_event.target_packet_id, 456)
        self.assertEqual(reaction_event.emoji, "🔥")

    def test_build_text_event_ignores_reaction_packets(self):
        app = MeshgramApp(self._settings())
        packet = {
            "fromId": "!1234abcd",
            "channel": 1,
            "id": 790,
            "decoded": {
                "portnum": "TEXT_MESSAGE_APP",
                "payload": "hello".encode("utf-8"),
                "emoji": ord("❤"),
                "replyId": 456,
            },
        }

        event = app._build_meshtastic_event(packet)
        self.assertIsNone(event)

    def test_build_reaction_event_falls_back_to_payload_when_emoji_is_modifier_only(self):
        app = MeshgramApp(self._settings())
        packet = {
            "fromId": "!1234abcd",
            "channel": 1,
            "id": 791,
            "decoded": {
                "portnum": "TEXT_MESSAGE_APP",
                "payload": "❤️".encode("utf-8"),
                "emoji": 0xFE0F,
                "replyId": 456,
            },
        }

        reaction_event = app._build_meshtastic_reaction_event(packet)
        self.assertIsNotNone(reaction_event)
        assert reaction_event is not None
        self.assertEqual(reaction_event.target_packet_id, 456)
        self.assertEqual(reaction_event.emoji, "❤️")

    def test_build_reaction_event_ignores_modifier_only_emoji_without_payload_fallback(self):
        app = MeshgramApp(self._settings())
        packet = {
            "fromId": "!1234abcd",
            "channel": 1,
            "id": 792,
            "decoded": {
                "portnum": "TEXT_MESSAGE_APP",
                "payload": b"",
                "emoji": 0xFE0F,
                "replyId": 456,
            },
        }

        reaction_event = app._build_meshtastic_reaction_event(packet)
        self.assertIsNone(reaction_event)

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

    def test_send_text_disables_want_ack_for_broadcast_destination(self):
        client = MeshtasticClient(self._settings())
        iface = _SendTextWithReplyId()
        client.iface = iface

        result = client.send_text(
            SendMeshtasticAction(
                text="broadcast",
                destination_id=None,
                channel_index=2,
                want_ack=True,
            )
        )

        self.assertEqual(result["id"], 123)
        self.assertEqual(len(iface.calls), 1)
        self.assertEqual(iface.calls[0]["wantAck"], False)

    def test_send_text_falls_back_when_reply_id_is_unsupported(self):
        client = MeshtasticClient(self._settings())
        iface = _SendTextWithoutReplyId()
        client.iface = iface

        result = client.send_text(
            SendMeshtasticAction(
                text="first",
                channel_index=1,
                reply_id=111,
            )
        )

        self.assertEqual(result["id"], 321)
        self.assertEqual(client._supports_sendtext_reply_id, False)
        self.assertEqual(len(iface.calls), 1)
        self.assertEqual(iface.calls[0]["text"], "first")

    def test_send_text_falls_back_to_send_data_with_reply_id(self):
        client = MeshtasticClient(self._settings())
        iface = _SendTextWithoutReplyIdWithSendDataReplyId()
        client.iface = iface

        result = client.send_text(
            SendMeshtasticAction(
                text="payload",
                channel_index=1,
                reply_id=333,
            )
        )

        self.assertEqual(result["id"], 202)
        self.assertEqual(client._supports_sendtext_reply_id, False)
        self.assertTrue(client._supports_senddata_reply_id)
        self.assertEqual(len(iface.send_data_calls), 1)
        self.assertEqual(iface.send_data_calls[0]["replyId"], 333)
        self.assertEqual(iface.send_data_calls[0]["data"], b"payload")

    def test_send_text_falls_back_to_low_level_packet_when_send_data_reply_id_unsupported(self):
        client = MeshtasticClient(self._settings())
        iface = _SendTextAndSendDataWithoutReplyIdWithLowLevelPacket()
        client.iface = iface

        result = client.send_text(
            SendMeshtasticAction(
                text="hello",
                destination_id="!1234abcd",
                channel_index=4,
                reply_id=444,
                want_ack=True,
            )
        )

        self.assertEqual(getattr(result, "id", None), 4242)
        self.assertEqual(client._supports_sendtext_reply_id, False)
        self.assertEqual(client._supports_senddata_reply_id, False)
        self.assertEqual(len(iface.send_packet_calls), 1)
        packet = iface.send_packet_calls[0]["packet"]
        self.assertEqual(packet.channel, 4)
        self.assertEqual(packet.decoded.reply_id, 444)
        self.assertEqual(packet.decoded.payload.decode("utf-8"), "hello")
        self.assertEqual(iface.send_packet_calls[0]["wantAck"], True)

    def test_send_reaction_uses_low_level_packet_when_available(self):
        client = MeshtasticClient(self._settings())
        iface = _SendTextAndSendDataWithoutReplyIdWithLowLevelPacket()
        client.iface = iface

        result = client.send_reaction(
            SendMeshtasticReactionAction(
                emoji="❤",
                target_packet_id=88,
                channel_index=2,
            )
        )

        self.assertEqual(getattr(result, "id", None), 4242)
        self.assertEqual(len(iface.send_packet_calls), 1)
        packet = iface.send_packet_calls[0]["packet"]
        self.assertEqual(packet.decoded.reply_id, 88)
        self.assertEqual(packet.decoded.emoji, ord("❤"))
        self.assertEqual(packet.decoded.payload.decode("utf-8"), "❤")

    def test_send_reaction_falls_back_to_plain_text_when_low_level_unavailable(self):
        client = MeshtasticClient(self._settings())
        iface = _SendTextForReactionFallback()
        client.iface = iface
        client._supports_lowlevel_packet = False

        result = client.send_reaction(
            SendMeshtasticReactionAction(
                emoji="❤",
                target_packet_id=55,
                channel_index=3,
            )
        )

        self.assertEqual(result["id"], 777)
        self.assertEqual(len(iface.calls), 1)
        self.assertEqual(iface.calls[0]["text"], "❤")
        self.assertEqual(iface.calls[0]["channelIndex"], 3)


if __name__ == "__main__":
    unittest.main()
