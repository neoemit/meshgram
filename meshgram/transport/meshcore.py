from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
import uuid
from typing import Any, Awaitable, Callable, Optional

from ..config import MeshgramSettings
from ..types import MeshPacketRef, MeshTextEvent, SendMeshAction

LOGGER = logging.getLogger(__name__)

MeshTextCallback = Callable[[MeshTextEvent], Awaitable[None]]
# Receives the payload dict of every MeshCore RX_LOG_DATA event (raw RF packet
# plus SNR/RSSI). Used by observer-style extensions such as MeshMapper uploads.
RxLogListener = Callable[[dict[str, Any]], Awaitable[None]]


DEFAULT_MESHCORE_PAYLOAD_LIMIT = 140
DEFAULT_OUTBOUND_ECHO_TEXT_FALLBACK_TTL_SECONDS = 2.0
# Channel slots on firmware that doesn't report how many it has.
DEFAULT_MAX_CHANNELS = 8

# Friendlier text for the companion protocol's error codes.
_ERROR_TEXT = {
    "ERR_CODE_UNSUPPORTED_CMD": "the radio's firmware doesn't support this",
    "ERR_CODE_NOT_FOUND": "the radio doesn't have it",
    "ERR_CODE_TABLE_FULL": "the radio has no room left",
    "ERR_CODE_BAD_STATE": "the radio can't do this right now",
    "ERR_CODE_FILE_IO_ERROR": "the radio couldn't save it",
    "ERR_CODE_ILLEGAL_ARG": "the radio rejected the value",
    "timeout": "the radio didn't answer in time",
    "no_event_received": "the radio didn't answer",
}


class RadioCommandError(RuntimeError):
    """A command for the radio failed, or the radio isn't connected."""


def describe_radio_error(payload: Any) -> str:
    if isinstance(payload, dict):
        code = payload.get("code_string") or payload.get("reason") or payload.get("error")
        if code is None and payload.get("error_code") is not None:
            code = f"error code {payload['error_code']}"
        if code is not None:
            return _ERROR_TEXT.get(str(code), str(code))
    return "the radio reported an error"


class MeshCoreTransport:
    """The MeshCore companion radio, via the ``meshcore`` library.

    Normalizes incoming messages into ``MeshTextEvent`` objects, sends
    ``SendMeshAction`` messages, and gives plugins and the web app's control
    panel access to the radio (raw RF logs, contacts, channels, commands).
    """

    def __init__(self, settings: MeshgramSettings):
        self.settings = settings
        self.local_node_id: Optional[str] = None
        self._mc: Any = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._on_text: Optional[MeshTextCallback] = None
        self._contacts: dict[str, dict[str, Any]] = {}
        self._channels: dict[int, dict[str, Any]] = {}
        self._device_info: dict[str, Any] = {}
        self._subscriptions: list[Any] = []
        # One command at a time: the library matches replies to commands by event type only.
        self._command_lock = asyncio.Lock()
        self.local_short_name: Optional[str] = None
        # Optional fallback cache for identity-less echoes:
        # (channel, text) -> monotonic-time-sent.
        self._recent_outbound_texts: dict[tuple[int, str], float] = {}
        self._rx_log_listeners: list[RxLogListener] = []
        self._remote_rx_log_listeners: list[RxLogListener] = []

    # --- Lifecycle ----------------------------------------------------------

    @property
    def payload_limit(self) -> int:
        return DEFAULT_MESHCORE_PAYLOAD_LIMIT

    @property
    def is_connected(self) -> bool:
        if self._mc is None:
            return False
        flag = getattr(self._mc, "is_connected", None)
        if isinstance(flag, bool):
            return flag
        return True

    async def connect(self, loop: asyncio.AbstractEventLoop, on_text: MeshTextCallback) -> None:
        try:
            from meshcore import EventType, MeshCore
        except ImportError as exc:  # pragma: no cover - guarded import
            raise RuntimeError(
                "The `meshcore` package is required; install with: pip install meshcore>=2.3.7"
            ) from exc

        self._loop = loop
        self._on_text = on_text

        cfg = self.settings.meshcore.connection
        if cfg.mode == "tcp":
            LOGGER.info("Connecting to MeshCore over TCP: %s:%s", cfg.tcp_host, cfg.tcp_port)
            self._mc = await MeshCore.create_tcp(
                cfg.tcp_host,
                cfg.tcp_port,
                auto_reconnect=cfg.auto_reconnect,
            )
        elif cfg.mode == "ble":
            if not cfg.ble_address:
                raise ValueError("MeshCore BLE mode requires meshcore.connection.ble_address")
            LOGGER.info("Connecting to MeshCore over BLE: %s", cfg.ble_address)
            ble_kwargs: dict[str, Any] = {}
            if cfg.ble_pin:
                ble_kwargs["pin"] = cfg.ble_pin
            self._mc = await MeshCore.create_ble(cfg.ble_address, **ble_kwargs)
        else:
            if cfg.serial_device:
                LOGGER.info(
                    "Connecting to MeshCore serial device: %s (baudrate=%s)",
                    cfg.serial_device,
                    cfg.baudrate,
                )
            else:
                LOGGER.info("Connecting to MeshCore serial device via auto-detect")
            self._mc = await MeshCore.create_serial(cfg.serial_device, cfg.baudrate)

        if self._mc is None or not getattr(self._mc, "is_connected", False) or getattr(self._mc, "commands", None) is None:
            handshake_hint = (
                "MeshCore handshake failed. Verify: (1) the device runs MeshCore companion "
                "firmware compiled with the matching transport; (2) meshcore.connection.baudrate "
                "matches (common values: 115200, 921600); (3) meshcore.connection.mode "
                "(serial/tcp/ble) is right for this device. Meshtastic radios aren't supported."
            )
            self._mc = None
            raise RuntimeError(handshake_hint)

        self._enable_channel_log_path_enrichment()

        async with self._command_lock:
            await self._refresh_local_node_async()
            self._device_info = await self._query_device_info()
            await self._refresh_channels_async()
            await self._refresh_contacts_async()

        self._subscriptions.append(
            self._mc.subscribe(EventType.CONTACT_MSG_RECV, self._handle_contact_msg)
        )
        self._subscriptions.append(
            self._mc.subscribe(EventType.CHANNEL_MSG_RECV, self._handle_channel_msg)
        )
        self._subscriptions.append(
            self._mc.subscribe(EventType.NEW_CONTACT, self._handle_new_contact)
        )
        rx_log_event = getattr(EventType, "RX_LOG_DATA", None)
        if rx_log_event is not None:
            self._subscriptions.append(
                self._mc.subscribe(rx_log_event, self._handle_rx_log)
            )

        await self._mc.start_auto_message_fetching()
        LOGGER.info("MeshCore transport ready (local_node_id=%s, contacts=%s)", self.local_node_id, len(self._contacts))

    def invalidate_connection(self) -> None:
        """Tear down any active connection. Idempotent."""
        for sub in self._subscriptions:
            with contextlib.suppress(Exception):
                self._mc.unsubscribe(sub)
        self._subscriptions = []
        if self._mc is not None and self._loop is not None and not self._loop.is_closed():
            stopper = getattr(self._mc, "stop_auto_message_fetching", None)
            if callable(stopper):
                with contextlib.suppress(Exception):
                    coro = stopper()
                    if asyncio.iscoroutine(coro):
                        self._loop.create_task(coro)
            disconnect = getattr(self._mc, "disconnect", None)
            if callable(disconnect):
                with contextlib.suppress(Exception):
                    coro = disconnect()
                    if asyncio.iscoroutine(coro):
                        self._loop.create_task(coro)
        self._mc = None
        self.local_node_id = None
        self.local_short_name = None
        self._device_info = {}
        self._recent_outbound_texts.clear()

    def close(self) -> None:
        self.invalidate_connection()

    def _enable_channel_log_path_enrichment(self) -> None:
        set_decrypt_channel_logs = getattr(self._mc, "set_decrypt_channel_logs", None)
        if not callable(set_decrypt_channel_logs):
            LOGGER.debug("MeshCore SDK does not expose channel-log path enrichment")
            return

        try:
            set_decrypt_channel_logs(True)
        except Exception as exc:  # pragma: no cover - defensive for SDK/device quirks
            LOGGER.warning("MeshCore channel-log path enrichment could not be enabled: %s", exc)
        else:
            LOGGER.info("MeshCore channel-log path enrichment enabled")

    def refresh_local_node_id(self) -> None:
        # Sync-callable shim from app; the real refresh is async and runs at connect.
        if self._mc is None:
            return
        info = getattr(self._mc, "self_info", None)
        if isinstance(info, dict):
            self.local_node_id = self._derive_local_node_id(info)

    async def _refresh_local_node_async(self) -> None:
        info = getattr(self._mc, "self_info", None)
        if isinstance(info, dict):
            self.local_node_id = self._derive_local_node_id(info)
            for key in ("adv_name", "name", "shortName", "short_name"):
                value = info.get(key)
                if isinstance(value, str) and value.strip():
                    self.local_short_name = value.strip()
                    break

    async def _refresh_channels_async(self) -> None:
        """Load MeshCore channel secrets so RF logs can be matched to channel messages.

        meshcore_py can only add the repeater hash path to CHANNEL_MSG_RECV
        events after it has channel information in its packet parser. The
        companion receive frame itself exposes only the hop count; the full path
        comes from decrypting correlated RF LOG_DATA entries.
        """
        get_channel: Any = getattr(getattr(self._mc, "commands", None), "get_channel", None)
        if not callable(get_channel):
            LOGGER.debug("MeshCore SDK does not expose get_channel; path hashes may be unavailable")
            return

        from meshcore import EventType  # type: ignore[attr-defined]

        loaded = 0
        channels: dict[int, dict[str, Any]] = {}
        for channel_index in range(self.max_channels):
            try:
                result = await get_channel(channel_index)
            except Exception as exc:
                LOGGER.debug("MeshCore get_channel(%s) failed: %s", channel_index, exc)
                continue

            if getattr(result, "type", None) == EventType.ERROR:
                LOGGER.debug("MeshCore get_channel(%s) returned error: %s", channel_index, result.payload)
                continue
            if getattr(result, "type", None) is not None:
                loaded += 1
                payload = getattr(result, "payload", None)
                if isinstance(payload, dict):
                    channels[channel_index] = dict(payload)

        self._channels = channels
        LOGGER.info("MeshCore channel metadata refreshed (slots=%s)", loaded)

    async def _refresh_contacts_async(self) -> None:
        try:
            result = await self._mc.commands.get_contacts()
        except Exception as exc:
            LOGGER.warning("MeshCore get_contacts failed: %s", exc)
            return
        if getattr(result, "type", None) is None:
            return
        from meshcore import EventType

        if result.type == EventType.ERROR:
            LOGGER.warning("MeshCore get_contacts returned error: %s", result.payload)
            return
        payload = result.payload
        if isinstance(payload, dict):
            self._contacts = dict(payload)

    # --- Commands (control panel) --------------------------------------------

    async def command(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """Run a ``meshcore`` library command (``MeshCore.commands.<name>``); returns its payload.

        Raises ``RadioCommandError`` when the radio isn't connected, the library
        lacks the command, or the radio answers with an error.
        """
        async with self._command_lock:
            return await self._command(name, *args, **kwargs)

    async def _command(self, name: str, *args: Any, **kwargs: Any) -> Any:
        if self._mc is None:
            raise RadioCommandError("The radio isn't connected")
        method = getattr(getattr(self._mc, "commands", None), name, None)
        if not callable(method):
            raise RadioCommandError(f"The installed meshcore library has no {name} command")

        from meshcore import EventType

        try:
            result = await method(*args, **kwargs)
        except (ValueError, TypeError) as exc:
            raise RadioCommandError(str(exc)) from exc
        if getattr(result, "type", None) == EventType.ERROR:
            raise RadioCommandError(describe_radio_error(getattr(result, "payload", None)))
        return getattr(result, "payload", None)

    async def refresh_channels(self) -> None:
        async with self._command_lock:
            await self._refresh_channels_async()

    async def refresh_contacts(self) -> None:
        async with self._command_lock:
            await self._refresh_contacts_async()

    async def refresh_self_info(self) -> dict[str, Any]:
        """Ask the radio for its SELF_INFO again (name, position, radio settings) after changing them."""
        async with self._command_lock:
            await self._command("send_appstart")
            await self._refresh_local_node_async()
        return self.device_self_info

    @staticmethod
    def _derive_local_node_id(info: dict[str, Any]) -> Optional[str]:
        pubkey = info.get("public_key")
        if isinstance(pubkey, str) and pubkey:
            return pubkey[:12]
        if isinstance(pubkey, (bytes, bytearray)):
            return pubkey.hex()[:12]
        return None

    # --- Device access for extensions ---------------------------------------

    @property
    def device_self_info(self) -> dict[str, Any]:
        """SELF_INFO reported by the companion radio (name, public key, radio params)."""
        info = getattr(self._mc, "self_info", None) if self._mc is not None else None
        return dict(info) if isinstance(info, dict) else {}

    @property
    def contacts(self) -> dict[str, dict[str, Any]]:
        """Contacts known to the companion radio, keyed by public key (hex)."""
        return {key: dict(value) for key, value in self._contacts.items() if isinstance(value, dict)}

    @property
    def channels(self) -> list[dict[str, Any]]:
        """Configured channels with their secrets (``name``, ``secret``, ``hash``), for decrypting RF logs."""
        result = []
        for _, channel in sorted(self._channels.items()):
            secret = channel.get("channel_secret")
            name = str(channel.get("channel_name") or "").strip()
            if not isinstance(secret, (bytes, bytearray)) or len(secret) != 16 or not any(secret) or not name:
                continue
            secret = bytes(secret)
            channel_hash = str(channel.get("channel_hash") or hashlib.sha256(secret).hexdigest()[:2]).lower()
            result.append({"name": name, "secret": secret, "hash": channel_hash})
        return result

    @property
    def device_info(self) -> dict[str, Any]:
        """DEVICE_INFO read at connect (model, firmware ``ver``, ``max_channels``, ...); may be empty."""
        return dict(self._device_info)

    @property
    def max_channels(self) -> int:
        try:
            reported = int(self._device_info.get("max_channels") or 0)
        except (TypeError, ValueError):
            reported = 0
        return reported if reported > 0 else DEFAULT_MAX_CHANNELS

    @property
    def channel_slots(self) -> list[dict[str, Any]]:
        """Every channel slot read from the radio, empty ones included: ``index``, ``name``, ``secret``, ``hash``."""
        slots = []
        for index, channel in sorted(self._channels.items()):
            secret = channel.get("channel_secret")
            secret = bytes(secret) if isinstance(secret, (bytes, bytearray)) and len(secret) == 16 else bytes(16)
            slots.append(
                {
                    "index": index,
                    "name": str(channel.get("channel_name") or "").strip(),
                    "secret": secret,
                    "hash": hashlib.sha256(secret).hexdigest()[:2],
                }
            )
        return slots

    def add_rx_log_listener(self, listener: RxLogListener) -> None:
        """Register a callback for raw RF packet logs. Survives reconnects."""
        if listener not in self._rx_log_listeners:
            self._rx_log_listeners.append(listener)

    def remove_rx_log_listener(self, listener: RxLogListener) -> None:
        with contextlib.suppress(ValueError):
            self._rx_log_listeners.remove(listener)

    # Packets heard by *other* observers (e.g. relayed from MeshMapper by the meshmapper
    # plugin) are shared with plugins through the transport, like local RF logs. Each dict
    # looks like an RF log (``payload``, ``snr``, ``rssi``) plus ``observer_id`` and
    # ``observer_name``; when only packet metadata is known (MeshMapper's live feed), it has
    # ``decoded`` (header fields and path, as ``meshcore_packets.decode_packet`` returns them)
    # instead of ``payload``, and optionally ``source_node`` (``public_key``, ``name``,
    # ``lat``, ``lon``) when the sender is known.

    def add_remote_rx_log_listener(self, listener: RxLogListener) -> None:
        if listener not in self._remote_rx_log_listeners:
            self._remote_rx_log_listeners.append(listener)

    def remove_remote_rx_log_listener(self, listener: RxLogListener) -> None:
        with contextlib.suppress(ValueError):
            self._remote_rx_log_listeners.remove(listener)

    async def dispatch_remote_rx_log(self, rx_log: dict[str, Any]) -> None:
        for listener in list(self._remote_rx_log_listeners):
            try:
                await listener(dict(rx_log))
            except Exception:
                LOGGER.exception("MeshCore remote RX log listener failed")

    async def query_device_info(self) -> dict[str, Any]:
        """Return the DEVICE_INFO payload (model, firmware version) or ``{}``."""
        async with self._command_lock:
            self._device_info = await self._query_device_info()
        return dict(self._device_info)

    async def _query_device_info(self) -> dict[str, Any]:
        commands = getattr(self._mc, "commands", None)
        send_device_query = getattr(commands, "send_device_query", None)
        if not callable(send_device_query):
            return {}

        from meshcore import EventType

        try:
            result = await send_device_query()
        except Exception as exc:
            LOGGER.debug("MeshCore device query failed: %s", exc)
            return {}
        if getattr(result, "type", None) == EventType.ERROR:
            return {}
        payload = getattr(result, "payload", None)
        return dict(payload) if isinstance(payload, dict) else {}

    async def sign_with_device(self, data: bytes) -> bytes:
        """Sign ``data`` with the radio's identity key (Ed25519) without exporting it."""
        if self._mc is None:
            raise RuntimeError("MeshCore client is not connected")
        sign = getattr(self._mc.commands, "sign", None)
        if not callable(sign):
            raise RuntimeError("Installed meshcore SDK does not support on-device signing")

        from meshcore import EventType

        async with self._command_lock:
            result = await sign(data)
        if getattr(result, "type", None) == EventType.ERROR:
            raise RuntimeError(f"MeshCore on-device signing failed: {getattr(result, 'payload', None)}")
        payload = getattr(result, "payload", None)
        signature = payload.get("signature") if isinstance(payload, dict) else None
        if isinstance(signature, str):
            signature = bytes.fromhex(signature)
        if not isinstance(signature, (bytes, bytearray)) or len(signature) != 64:
            raise RuntimeError("MeshCore on-device signing returned no valid signature")
        return bytes(signature)

    # --- Inbound event handlers --------------------------------------------

    async def _handle_rx_log(self, event: Any) -> None:
        if not self._rx_log_listeners:
            return
        payload = getattr(event, "payload", None)
        if not isinstance(payload, dict):
            return
        for listener in list(self._rx_log_listeners):
            try:
                await listener(dict(payload))
            except Exception:
                LOGGER.exception("MeshCore RX log listener failed")

    async def _handle_contact_msg(self, event: Any) -> None:
        payload = getattr(event, "payload", {}) or {}
        LOGGER.info("MeshCore inbound DM payload keys=%s", sorted(payload.keys()) if isinstance(payload, dict) else type(payload).__name__)
        text = str(payload.get("text", "")).strip()
        if not text:
            return

        pubkey_prefix = str(payload.get("pubkey_prefix", "") or "").strip().lower()
        sender_label = self.resolve_sender_label(pubkey_prefix or None)
        timestamp = payload.get("timestamp")

        mesh_event = MeshTextEvent(
            from_id=pubkey_prefix or None,
            to_id=self.local_node_id,
            packet_id=self._synthetic_inbound_id("dm", pubkey_prefix, text, timestamp),
            channel_index=-1,
            text=text,
            sender_label=sender_label,
            raw_packet=dict(payload),
        )

        if self._on_text is not None:
            await self._on_text(mesh_event)

    async def _handle_channel_msg(self, event: Any) -> None:
        payload = getattr(event, "payload", {}) or {}
        raw_text = str(payload.get("text", "")).strip()
        if not raw_text:
            return

        try:
            channel_index = int(payload.get("channel_idx", 0))
        except (TypeError, ValueError):
            channel_index = 0

        # MeshCore channel messages embed the sender in the body as "<name>: <text>".
        # Strip it so plugins see just the message; surface the name as sender_label.
        embedded_sender, body = _split_embedded_sender(raw_text)

        pubkey_prefix = str(payload.get("pubkey_prefix", "") or "").strip().lower()
        if embedded_sender:
            sender_label = embedded_sender
        elif pubkey_prefix:
            sender_label = self.resolve_sender_label(pubkey_prefix)
        else:
            sender_label = self._channel_sender_label(body, channel_index)
        timestamp = payload.get("sender_timestamp") or payload.get("timestamp")

        echo_reason = self._local_echo_reason(
            channel_index=channel_index,
            sender_pubkey_prefix=pubkey_prefix or None,
            embedded_sender=embedded_sender,
            body=body,
        )
        if echo_reason is not None:
            LOGGER.debug(
                "Suppressing MeshCore channel echo of our own transmission: "
                "reason=%s channel=%s pubkey_prefix=%s sender=%r text=%r",
                echo_reason,
                channel_index,
                pubkey_prefix or None,
                embedded_sender,
                raw_text[:80],
            )
            return

        LOGGER.info(
            "MeshCore channel message: channel=%s sender=%r text=%r",
            channel_index,
            sender_label,
            body[:80],
        )

        mesh_event = MeshTextEvent(
            from_id=pubkey_prefix or None,
            to_id=None,
            packet_id=self._synthetic_inbound_id("ch", str(channel_index), body, timestamp),
            channel_index=channel_index,
            text=body,
            sender_label=sender_label,
            raw_packet=dict(payload),
        )

        if self._on_text is not None:
            await self._on_text(mesh_event)

    async def _handle_new_contact(self, event: Any) -> None:
        payload = getattr(event, "payload", {}) or {}
        if not isinstance(payload, dict):
            return
        public_key = payload.get("public_key")
        if isinstance(public_key, str) and public_key:
            self._contacts[public_key] = dict(payload)

    @staticmethod
    def _synthetic_inbound_id(
        kind: str,
        scope: str,
        text: str,
        timestamp: Any,
    ) -> str:
        ts_part = str(timestamp) if timestamp is not None else f"now:{int(time.time() * 1000)}"
        digest = hashlib.sha1(f"{kind}|{scope}|{ts_part}|{text}".encode("utf-8")).hexdigest()[:16]
        return f"mc-{kind}-{digest}"

    # --- Outbound ----------------------------------------------------------

    async def asend_text(self, action: SendMeshAction) -> MeshPacketRef:
        """Send a channel or direct message; returns an identifier for it."""
        if self._mc is None:
            raise RuntimeError("MeshCore client is not connected")

        from meshcore import EventType

        is_dm = isinstance(action.destination_id, str) and bool(action.destination_id.strip())
        channel_index = action.channel_index if action.channel_index >= 0 else self.settings.meshcore.bridge_channel
        async with self._command_lock:
            if is_dm:
                result = await self._mc.commands.send_msg(action.destination_id, action.text)
            else:
                result = await self._mc.commands.send_chan_msg(channel_index, action.text)

            if getattr(result, "type", None) == EventType.ERROR:
                raise RuntimeError(f"MeshCore send failed: {describe_radio_error(result.payload)}")

            expected_ack = self._extract_expected_ack_hex(result)
            if action.wait_for_ack and action.want_ack and is_dm and expected_ack is not None:
                timeout_seconds = max(1.0, action.ack_timeout_ms / 1000) if action.ack_timeout_ms else 10.0
                ack_event = await self._mc.wait_for_event(
                    EventType.ACK,
                    attribute_filters={"code": expected_ack},
                    timeout=timeout_seconds,
                )
                if ack_event is None:
                    raise TimeoutError(f"MeshCore ACK wait timed out for code {expected_ack}")

        # Remember the text so we suppress the radio's echo of our own transmission
        # when it arrives back as an inbound channel message.
        if not is_dm:
            self._record_outbound_text(channel_index, action.text)

        # Channel messages have no expected_ack (broadcasts aren't acknowledged).
        return expected_ack or f"mc-out-{uuid.uuid4().hex[:12]}"

    def _record_outbound_text(self, channel_index: int, text: str) -> None:
        if not self._outbound_echo_text_fallback_enabled():
            return

        normalized_text = text.strip()
        if not normalized_text:
            return

        now = time.monotonic()
        self._prune_outbound_cache(now)
        self._recent_outbound_texts[(channel_index, normalized_text)] = now

    def _prune_outbound_cache(self, now: float) -> None:
        ttl_seconds = self._outbound_echo_text_fallback_ttl_seconds()
        if ttl_seconds <= 0:
            self._recent_outbound_texts.clear()
            return

        cutoff = now - ttl_seconds
        stale = [key for key, ts in self._recent_outbound_texts.items() if ts < cutoff]
        for key in stale:
            self._recent_outbound_texts.pop(key, None)

    def _local_echo_reason(
        self,
        *,
        channel_index: int,
        sender_pubkey_prefix: Optional[str],
        embedded_sender: Optional[str],
        body: str,
    ) -> Optional[str]:
        normalized_local_node_id = self._normalize_pubkey_prefix(self.local_node_id)
        normalized_sender_pubkey = self._normalize_pubkey_prefix(sender_pubkey_prefix)
        if (
            normalized_local_node_id is not None
            and normalized_sender_pubkey is not None
            and (
                normalized_sender_pubkey.startswith(normalized_local_node_id)
                or normalized_local_node_id.startswith(normalized_sender_pubkey)
            )
        ):
            return "local_pubkey_prefix"

        normalized_embedded_sender = self._normalize_embedded_sender(embedded_sender)
        if normalized_embedded_sender and self.local_short_name:
            local_short_name = self.local_short_name.strip().lower()
            if local_short_name and normalized_embedded_sender == local_short_name:
                return "local_short_name"

        if (
            normalized_embedded_sender is not None
            and normalized_local_node_id is not None
            and (
                normalized_embedded_sender == normalized_local_node_id
                or normalized_embedded_sender == normalized_local_node_id[:12]
            )
        ):
            return "embedded_pubkey_prefix"

        if self._outbound_echo_text_fallback_enabled():
            self._prune_outbound_cache(time.monotonic())
            if (channel_index, body.strip()) in self._recent_outbound_texts:
                return "text_fallback"

        return None

    def _outbound_echo_text_fallback_enabled(self) -> bool:
        return bool(self.settings.meshcore.outbound_echo_text_fallback_enabled)

    def _outbound_echo_text_fallback_ttl_seconds(self) -> float:
        raw = self.settings.meshcore.outbound_echo_text_fallback_ttl_seconds
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return DEFAULT_OUTBOUND_ECHO_TEXT_FALLBACK_TTL_SECONDS

    @staticmethod
    def _normalize_pubkey_prefix(value: object) -> Optional[str]:
        if not isinstance(value, str):
            return None
        normalized = value.strip().lower()
        if normalized.startswith("!"):
            normalized = normalized[1:]
        if normalized.startswith("0x"):
            normalized = normalized[2:]
        return normalized or None

    @staticmethod
    def _normalize_embedded_sender(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.split("•", 1)[0].strip().lower()
        return normalized or None

    @staticmethod
    def _extract_expected_ack_hex(result: Any) -> Optional[str]:
        payload = getattr(result, "payload", None)
        if not isinstance(payload, dict):
            return None
        raw = payload.get("expected_ack")
        if isinstance(raw, (bytes, bytearray)):
            return raw.hex()
        if isinstance(raw, str):
            return raw
        return None

    # --- Sender labels ------------------------------------------------------

    def resolve_sender_label(self, from_id: Optional[str]) -> str:
        if from_id:
            override = self.settings.meshcore.contact_name_overrides
            normalized = from_id.strip().lower()
            for key, value in override.items():
                if str(key).strip().lower() == normalized or normalized.startswith(str(key).strip().lower()):
                    return value

            for pubkey, contact in self._contacts.items():
                pk = pubkey.lower() if isinstance(pubkey, str) else ""
                if pk.startswith(normalized) or normalized.startswith(pk[:12]):
                    name = contact.get("adv_name")
                    if isinstance(name, str) and name.strip():
                        return name.strip()

            return from_id

        return "unknown"

    @staticmethod
    def _channel_sender_label(text: str, channel_index: int) -> str:  # noqa: ARG004 - reserved for future heuristics
        return f"ch{channel_index}"


def _split_embedded_sender(raw_text: str) -> tuple[Optional[str], str]:
    """MeshCore channel messages arrive as ``"<short_name>: <text>"``. Split them."""
    delimiter = ": "
    idx = raw_text.find(delimiter)
    if idx <= 0 or idx > 32:
        return None, raw_text
    prefix = raw_text[:idx].strip()
    if not prefix or "\n" in prefix or "\r" in prefix:
        return None, raw_text
    body = raw_text[idx + len(delimiter):].strip()
    if not body:
        return None, raw_text
    return prefix, body
