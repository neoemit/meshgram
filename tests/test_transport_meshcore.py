from __future__ import annotations

import asyncio
import sys
import types
import unittest
from typing import Any

from meshgram.config import MeshgramSettings
from meshgram.types import SendMeshAction, SendMeshReactionAction


def _install_meshcore_stub() -> tuple[types.ModuleType, type, type, list]:
    """Inject a minimal stub for the ``meshcore`` package into ``sys.modules``."""

    sent: list[dict[str, Any]] = []

    class _EventType:
        ERROR = "ERROR"
        MSG_SENT = "MSG_SENT"
        MSG_OK = "MSG_OK"
        ACK = "ACK"
        CONTACT_MSG_RECV = "CONTACT_MSG_RECV"
        CHANNEL_MSG_RECV = "CHANNEL_MSG_RECV"
        NEW_CONTACT = "NEW_CONTACT"
        CONTACTS = "CONTACTS"

    class _Event:
        def __init__(self, type_: str, payload: Any):
            self.type = type_
            self.payload = payload

    class _Commands:
        def __init__(self, sink: list[dict[str, Any]]):
            self._sink = sink

        async def send_msg(self, dst: Any, msg: str, timestamp: Any = None) -> _Event:
            self._sink.append({"kind": "dm", "dst": dst, "msg": msg})
            return _Event(_EventType.MSG_SENT, {"expected_ack": b"\xde\xad\xbe\xef"})

        async def send_chan_msg(self, chan: int, msg: str, timestamp: Any = None) -> _Event:
            self._sink.append({"kind": "channel", "channel": chan, "msg": msg})
            return _Event(_EventType.MSG_OK, {})

        async def get_contacts(self, lastmod: int = 0) -> _Event:
            return _Event(_EventType.CONTACTS, {"deadbeefdeadbeef": {"adv_name": "Alice", "public_key": "deadbeefdeadbeef"}})

    class _MeshCore:
        def __init__(self):
            self.commands = _Commands(sent)
            self.self_info = {"public_key": "deadbeefdeadbeef0000"}
            self.is_connected = True
            self._auto_fetching = False
            self.subscribed: list[tuple[str, Any]] = []

        @classmethod
        async def create_serial(cls, device, baudrate=115200, debug=False):
            return cls()

        @classmethod
        async def create_tcp(cls, host, port, auto_reconnect=True, max_reconnect_attempts=3):
            return cls()

        @classmethod
        async def create_ble(cls, address, pin=None):
            return cls()

        def subscribe(self, event_type, callback, attribute_filters=None):
            sub = (event_type, callback)
            self.subscribed.append(sub)
            return sub

        def unsubscribe(self, sub):
            self.subscribed.remove(sub)

        async def start_auto_message_fetching(self):
            self._auto_fetching = True

        async def stop_auto_message_fetching(self):
            self._auto_fetching = False

        async def disconnect(self):
            self.is_connected = False

        async def wait_for_event(self, event_type, attribute_filters=None, timeout=None):
            return _Event(_EventType.ACK, {"code": (attribute_filters or {}).get("code", "")})

    stub = types.ModuleType("meshcore")
    stub.EventType = _EventType
    stub.MeshCore = _MeshCore
    sys.modules["meshcore"] = stub
    # Drop any cached transport so the next import uses the stub.
    sys.modules.pop("meshgram.transport.meshcore", None)
    return stub, _MeshCore, _EventType, sent


class MeshCoreTransportTests(unittest.TestCase):
    def setUp(self):
        self._stub, self._mesh_cls, self._event_cls, self._sent = _install_meshcore_stub()
        self.settings = MeshgramSettings(
            telegram_bot_token="token",
            telegram_group_id=-100,
            config_path="config.yaml",
            plugins=[],
        )
        self.settings.mesh.backend = "meshcore"
        self.settings.meshcore.bridge_channel = 0
        self.settings.meshcore.connection.mode = "serial"

    def tearDown(self):
        sys.modules.pop("meshcore", None)
        sys.modules.pop("meshgram.transport.meshcore", None)

    def _make_transport(self):
        from meshgram.transport.meshcore import MeshCoreTransport

        return MeshCoreTransport(self.settings)

    def test_send_text_without_destination_uses_channel_send(self):
        transport = self._make_transport()
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(transport.connect(loop, _noop_callback, _noop_callback))
            action = SendMeshAction(text="hello mesh", channel_index=0)
            loop.run_until_complete(transport.asend_text(action))
        finally:
            loop.close()

        self.assertEqual(len(self._sent), 1)
        self.assertEqual(self._sent[0]["kind"], "channel")
        self.assertEqual(self._sent[0]["channel"], 0)
        self.assertEqual(self._sent[0]["msg"], "hello mesh")

    def test_send_text_with_destination_uses_dm_send(self):
        transport = self._make_transport()
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(transport.connect(loop, _noop_callback, _noop_callback))
            action = SendMeshAction(
                text="hi",
                destination_id="deadbeefdeadbeef",
                channel_index=-1,
            )
            result = loop.run_until_complete(transport.asend_text(action))
        finally:
            loop.close()

        self.assertEqual(len(self._sent), 1)
        self.assertEqual(self._sent[0]["kind"], "dm")
        self.assertEqual(self._sent[0]["dst"], "deadbeefdeadbeef")
        self.assertEqual(result, {"id": "deadbeef"})

    def test_send_reaction_is_dropped_with_debug_log(self):
        transport = self._make_transport()
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(transport.connect(loop, _noop_callback, _noop_callback))
            action = SendMeshReactionAction(emoji="❤", target_packet_id="mc-ch-abc123")
            with self.assertLogs("meshgram.transport.meshcore", level="DEBUG") as ctx:
                result = loop.run_until_complete(transport.asend_reaction(action))
        finally:
            loop.close()

        self.assertIsNone(result)
        self.assertTrue(any("does not support reactions" in line for line in ctx.output))

    def test_inbound_channel_message_dispatches_event(self):
        received: list = []

        async def collector(event):
            received.append(event)

        transport = self._make_transport()
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(transport.connect(loop, collector, _noop_callback))
            channel_callback = next(
                cb for (etype, cb) in transport._mc.subscribed if etype == self._event_cls.CHANNEL_MSG_RECV
            )
            event = self._stub_event(self._event_cls.CHANNEL_MSG_RECV, {"text": "hi", "channel_idx": 1})
            loop.run_until_complete(channel_callback(event))
        finally:
            loop.close()

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].text, "hi")
        self.assertEqual(received[0].channel_index, 1)

    def test_backend_capabilities(self):
        transport = self._make_transport()
        self.assertEqual(transport.backend_name, "meshcore")
        self.assertFalse(transport.supports_reactions)
        self.assertFalse(transport.supports_reply_threading)

    def _stub_event(self, event_type, payload):
        from meshcore import EventType  # noqa: F401 — ensures stub is active

        class _Evt:
            def __init__(self, t, p):
                self.type = t
                self.payload = p

        return _Evt(event_type, payload)


async def _noop_callback(_event):
    return None


if __name__ == "__main__":
    unittest.main()
