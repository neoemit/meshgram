"""The Telegram bridge: one Telegram group <-> one radio channel.

The plugin can run more than once (``bridge@<id>`` in config.yaml, or added in
the control panel): the plain ``bridge`` relays the main group, and each further
instance bridges its own channel to its own group, through the main bot or one
of its own (``telegram_bot_token``).

In "sos" mode the group isn't a chat at all: a SosDesk (meshgram/sos_desk.py) works a
MeshSOS channel with buttons. It shows the emergency with ACK and Resolved buttons, keeps
an SOS button pinned, and sends nothing that is typed to the radio.

In "chat" mode a sender prefix of ``<{display_name}> {message}`` sends a message the way
chat relay bots write it, which MeshSOS boards read as sent by that name (see MeshSOS's
SosProtocol.h).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from typing import Any, Optional

from meshgram import sos_desk
from meshgram.config import data_dir
from meshgram.plugin import CHANNEL_FORMAT, BasePlugin
from meshgram.status import CONNECTED, CONNECTING, DISCONNECTED
from meshgram.telegram_events import button_event_from_query, event_from_message
from meshgram.telegram_texts import TELEGRAM_TEXT_CACHE_SIZE
from meshgram.text_utils import split_for_mesh, utf8_len
from meshgram.types import (
    MeshTextEvent,
    PluginAction,
    PluginContext,
    SendMeshAction,
    SendTelegramAction,
    TelegramButtonEvent,
    TelegramMessageEvent,
    TelegramReactionEvent,
)

LOGGER = logging.getLogger(__name__)

# A sender prefix that names the sender like this sends "on behalf of" them.
RELAY_MARK = "<{display_name}>"
RELAY_TEMPLATE = "<{display_name}> {message}"
# What MeshSOS accepts as a name (SosProtocol.h: MAX_NAME_LEN, no ':' and no angle brackets).
MAX_NODE_NAME_LENGTH = 31

MODE_CHAT = "chat"
MODE_SOS = "sos"

LONG_SPLIT = "split"
LONG_TRUNCATE = "truncate"
ELLIPSIS = "…"

OWN_BOT_RETRY_SECONDS = 15.0
OWN_BOT_HEALTH_CHECK_SECONDS = 60.0


class BridgePlugin(BasePlugin):
    name = "bridge"
    title = "Telegram bridge"
    description = (
        "Relays messages between a Telegram group and one radio channel. Long Telegram "
        "messages are split into numbered chunks that fit in a radio packet. Emoji reactions "
        "in Telegram go out as a short message quoting the message reacted to (when Meshgram saw it). "
        "Add another bridge for a second channel and group; in MeshSOS mode that group works an "
        "SOS channel with buttons."
    )
    multi_instance = True
    settings_schema = {
        "type": "object",
        "properties": {
            "channel": {
                "type": "integer",
                "minimum": 0,
                "maximum": 255,
                "format": CHANNEL_FORMAT,
                "title": "Radio channel",
                "description": (
                    "The channel bridged to the Telegram group. Empty: meshcore.bridge_channel from "
                    "config.yaml (an added bridge must name its own)."
                ),
            },
            "mode": {
                "type": "string",
                "enum": [MODE_CHAT, MODE_SOS],
                "x-enum-titles": ["Chat: relay what people write", "MeshSOS: buttons only"],
                "default": MODE_CHAT,
                "title": "Mode",
                "description": (
                    "MeshSOS, for a channel of MeshSOS boards: an emergency appears in the group with ACK and "
                    "Resolved buttons, and a pinned message has the SOS button. A press goes out on the radio "
                    "under the presser's Telegram first name. Nothing typed in the group is sent, so a "
                    "stray message can't start an alarm; the sender prefix, long-message and reaction "
                    "settings below don't apply. Let the bot pin messages in the group."
                ),
            },
            "relay_reactions": {
                "type": "boolean",
                "default": True,
                "title": "Relay reactions",
                "description": (
                    "Send emoji reactions in Telegram to the radio channel as a message, e.g. "
                    "[Alice] reacted 👍 to \"[Bob] hello\". Telegram only tells the bot about "
                    "reactions when it's an admin of the group. Reactions to messages Meshgram "
                    "never saw the text of go out without the quote; custom emoji are left out. "
                    "Turn it off on an SOS channel, where any such message starts an alarm; a bridge "
                    "with a bot of its own relays messages only."
                ),
            },
            "telegram_group_id": {
                "type": "integer",
                "title": "Telegram group ID",
                "description": (
                    "The group this bridge relays. Empty: the main group (telegram.group_id). "
                    "Supergroups start with -100; the bot must be in the group and able to read its messages."
                ),
            },
            "telegram_bot_token": {
                "type": "string",
                "writeOnly": True,
                "pattern": r"^\d+:[A-Za-z0-9_-]+$",
                "x-pattern-message": "looks like 123456789:ABC… (from @BotFather)",
                "title": "Telegram bot token",
                "description": "A bot of its own for this bridge. Empty: the main bot (telegram.bot_token).",
            },
            "sender_prefix_template": {
                "type": "string",
                "title": "Sender prefix",
                "description": (
                    "How Telegram messages read on the radio; {display_name} and {message} are filled in. "
                    f"Empty: telegram.sender_prefix_template. With {RELAY_TEMPLATE} MeshSOS boards treat the "
                    "message as coming from that person (the MeshSOS mode does this by itself)."
                ),
            },
            "long_messages": {
                "type": "string",
                "enum": [LONG_SPLIT, LONG_TRUNCATE],
                "x-enum-titles": ["Split into numbered chunks", "Cut to one radio message"],
                "default": LONG_SPLIT,
                "title": "Long Telegram messages",
                "description": (
                    "Cut them when every radio message must stand on its own, as on an SOS channel: "
                    "a numbered chunk can't carry the sender's name the way MeshSOS reads it."
                ),
            },
        },
    }
    MIN_CHUNK_DELAY_MS = 900
    DEFAULT_SAFE_MAX_CHUNK_BYTES = 160

    def __init__(self, settings: dict[str, Any] | None = None):
        super().__init__(settings)
        self._context: Optional[PluginContext] = None
        self._own_bot_token = ""
        self._own_bot: Any = None  # telegram.ext.Application
        self._own_bot_task: Optional[asyncio.Task[None]] = None
        self._desk: Optional[sos_desk.SosDesk] = None

    # --- Settings ---------------------------------------------------------------------------------

    def _bridge_channel(self, context: PluginContext) -> int:
        configured_channel = self.settings.get("channel")
        if configured_channel is not None:
            try:
                return int(configured_channel)
            except (TypeError, ValueError):
                LOGGER.warning("bridge.settings.channel must be an integer; falling back to meshcore.bridge_channel")

        return context.settings.meshcore.bridge_channel

    def _group_id(self, context: PluginContext) -> int:
        configured = self.settings.get("telegram_group_id")
        if configured not in (None, ""):
            try:
                return int(configured)
            except (TypeError, ValueError):
                LOGGER.warning("%s: telegram_group_id must be an integer; using telegram.group_id", self.instance_name)
        return context.telegram_group_id

    def _configured_bot_token(self, context: PluginContext) -> str:
        """The token of this bridge's own bot; "" when it uses the main bot."""
        token = str(self.settings.get("telegram_bot_token") or "").strip()
        if not token or token == context.settings.telegram_bot_token:
            return ""
        return token

    @property
    def _sos_mode(self) -> bool:
        return self.settings.get("mode") == MODE_SOS

    def _template(self, context: PluginContext) -> str:
        if self._sos_mode:
            return RELAY_TEMPLATE
        template = self.settings.get("sender_prefix_template")
        if isinstance(template, str) and template.strip():
            return template
        return context.settings.telegram.sender_prefix_template

    def _payload_limit(self, context: PluginContext) -> int:
        chunking = context.settings.chunking
        # Channel messages are broadcasts: apply the broadcast size cap and pacing.
        payload_limit = context.mesh_payload_limit - max(0, chunking.payload_safety_margin_bytes)
        configured_max_chunk_bytes = chunking.max_chunk_bytes
        effective_max_chunk_bytes = (
            configured_max_chunk_bytes
            if configured_max_chunk_bytes > 0
            else self.DEFAULT_SAFE_MAX_CHUNK_BYTES
        )
        broadcast_cap = (
            chunking.broadcast_max_chunk_bytes
            if chunking.broadcast_max_chunk_bytes > 0
            else effective_max_chunk_bytes
        )
        effective_max_chunk_bytes = min(effective_max_chunk_bytes, broadcast_cap)
        return min(payload_limit, effective_max_chunk_bytes)

    def _with_sender_prefix(
        self, context: PluginContext, event: TelegramMessageEvent | TelegramReactionEvent, text: str
    ) -> str:
        template = self._template(context)
        display_name = self._radio_name(event, template)
        try:
            return template.format(display_name=display_name, message=text)
        except (KeyError, ValueError, IndexError):
            LOGGER.warning(
                "Invalid sender_prefix_template placeholders; expected display_name/message. Falling back."
            )
            return f"[{display_name}] {text}"

    # --- Lifecycle --------------------------------------------------------------------------------

    @property
    def _status_key(self) -> str:
        return f"telegram_{self.instance_id or self.name}"

    async def on_startup(self, context: PluginContext) -> list[PluginAction]:
        self._context = context
        if self.instance_id is not None and self.settings.get("channel") is None:
            # Without one it would relay the main bridge's channel a second time.
            raise ValueError("set its radio channel first")

        suffix = f"_{self.instance_id}" if self.instance_id else ""
        self._own_bot_token = self._configured_bot_token(context)
        if self._own_bot_token:
            self._set_bot_status(CONNECTING, "Starting bot")
            self._own_bot_task = asyncio.create_task(self._run_own_bot(self._own_bot_token))
        if self._sos_mode:
            self._desk = sos_desk.SosDesk(
                label=self.instance_name,
                state_path=data_dir() / f"bridge{suffix}_sos.json",
                bot=self._telegram_bot,
                chat_id=lambda: self._group_id(self._context),
                send_mesh=self._send_sos_text,
                radio_online=self._radio_online,
            )
            await self._desk.start()
        return []

    def _telegram_bot(self) -> Any:
        """The bot this bridge talks through; None while it isn't there."""
        if self._own_bot_token:
            return self._own_bot.bot if self._own_bot is not None else None
        return getattr(self._context, "telegram_bot", None)

    def _radio_online(self) -> bool:
        status = getattr(self._context, "status", None)
        return status is None or status.get("radio")["state"] == CONNECTED

    async def _send_sos_text(self, text: str) -> None:
        context = self._context
        if context is None or context.execute_actions is None:
            return
        chunking = context.settings.chunking
        action = SendMeshAction(
            text=text,
            channel_index=self._bridge_channel(context),
            retry_max_attempts=chunking.retry_max_attempts,
            retry_initial_delay_ms=chunking.retry_initial_delay_ms,
            retry_backoff_factor=chunking.retry_backoff_factor,
        )
        LOGGER.info("%s: sending on channel %s: %s", self.instance_name, action.channel_index, text)
        await context.execute_actions([action], self.instance_name)

    async def on_mesh_connected(self, transport: Any, context: PluginContext) -> None:
        self._context = context

    async def on_shutdown(self) -> None:
        desk, self._desk = self._desk, None
        if desk is not None:
            await desk.stop()
        task, self._own_bot_task = self._own_bot_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self._stop_own_bot()
        if self._own_bot_token:
            status = getattr(self._context, "status", None)
            if status is not None:
                status.remove(self._status_key)
        self._own_bot_token = ""

    async def apply_settings(self, settings: dict[str, Any]) -> bool:
        context = self._context
        if context is None:
            return False
        previous = self.settings
        self.settings = settings
        if (
            self._configured_bot_token(context) != self._own_bot_token
            or (self.instance_id is not None and settings.get("channel") is None)
            or self._sos_mode != (self._desk is not None)
        ):
            self.settings = previous
            return False  # Another bot (or none), another mode, or nothing to bridge: start over.
        return True

    # --- A bot of its own -------------------------------------------------------------------------

    def _set_bot_status(self, state: str, detail: str) -> None:
        status = getattr(self._context, "status", None)
        if status is not None:
            status.set_state(self._status_key, state, detail, label=f"Telegram ({self.instance_id or self.name})")

    async def _run_own_bot(self, token: str) -> None:
        """Start polling with the bridge's own bot; keeps trying while Telegram can't be reached."""
        from telegram import Update
        from telegram.error import InvalidToken, TelegramError
        from telegram.ext import ApplicationBuilder, CallbackQueryHandler, MessageHandler, filters

        while True:
            application = None
            try:
                application = ApplicationBuilder().token(token).build()
                # New messages only, as for the app's bot: an edit isn't relayed again.
                application.add_handler(
                    MessageHandler(
                        (filters.UpdateType.MESSAGE | filters.UpdateType.CHANNEL_POST) & ~filters.COMMAND,
                        self._on_own_bot_update,
                    )
                )
                application.add_handler(CallbackQueryHandler(self._on_own_bot_button))
                await application.initialize()
                await application.start()
                await application.updater.start_polling(
                    allowed_updates=Update.ALL_TYPES, error_callback=self._on_own_bot_polling_error
                )
            except InvalidToken:
                self._set_bot_status(DISCONNECTED, "Telegram doesn't accept this bot token")
                LOGGER.error("%s: Telegram doesn't accept its bot token; the bridge is down until it's changed", self.instance_name)
                await _shutdown_application(application)
                return
            except (TelegramError, OSError, TimeoutError) as exc:
                self._set_bot_status(DISCONNECTED, f"{exc}; retrying in {OWN_BOT_RETRY_SECONDS:.0f}s")
                LOGGER.warning("%s: its bot can't start (%s); retrying in %.0fs", self.instance_name, exc, OWN_BOT_RETRY_SECONDS)
                await _shutdown_application(application)
                await asyncio.sleep(OWN_BOT_RETRY_SECONDS)
                continue
            except asyncio.CancelledError:
                await _shutdown_application(application)
                raise
            self._own_bot = application
            username = application.bot.username
            self._set_bot_status(CONNECTED, f"@{username}" if username else "Bot API reachable")
            LOGGER.info("%s: polling with its own bot @%s", self.instance_name, username)
            break

        # Polling retries by itself; this notices when the bot is reachable again.
        while True:
            await asyncio.sleep(OWN_BOT_HEALTH_CHECK_SECONDS)
            try:
                me = await application.bot.get_me()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._set_bot_status(DISCONNECTED, f"Bot API unreachable: {exc}")
                continue
            self._set_bot_status(CONNECTED, f"@{me.username}" if me.username else "Bot API reachable")

    def _on_own_bot_polling_error(self, error: Exception) -> None:
        self._set_bot_status(DISCONNECTED, f"Polling failed: {error}")
        LOGGER.warning("%s: Telegram polling error: %s", self.instance_name, error)

    async def _stop_own_bot(self) -> None:
        application, self._own_bot = self._own_bot, None
        await _shutdown_application(application)

    async def _on_own_bot_update(self, update: Any, _context: Any) -> None:
        event = event_from_message(update.effective_message)
        context = self._context
        if event is None or context is None:
            return
        if self._own_bot is not None:
            # An update proves polling works (again).
            username = self._own_bot.bot.username
            self._set_bot_status(CONNECTED, f"@{username}" if username else "Bot API reachable")
        try:
            actions = await self._handle_telegram(event, context)
            if actions and context.execute_actions is not None:
                await context.execute_actions(actions, self.instance_name)
        except Exception:
            LOGGER.exception("%s failed handling a Telegram message", self.instance_name)

    async def _on_own_bot_button(self, update: Any, _context: Any) -> None:
        event = button_event_from_query(update.callback_query)
        context = self._context
        if event is None or context is None:
            return
        try:
            await self._handle_button(event, context)
        except Exception:
            LOGGER.exception("%s failed handling a Telegram button", self.instance_name)

    # --- Radio -> Telegram ------------------------------------------------------------------------

    async def on_mesh_message(
        self,
        event: MeshTextEvent,
        context: PluginContext,
    ) -> list[PluginAction]:
        self._context = context
        bridge_channel = self._bridge_channel(context)
        if event.channel_index != bridge_channel:
            return []

        if context.local_node_id and event.from_id == context.local_node_id:
            return []

        text = event.text.strip()
        if not text:
            return []

        if self._desk is not None:
            await self._desk.on_mesh(event.sender_label, text)
            return []
        action = SendTelegramAction(chat_id=self._group_id(context), text=f"[{event.sender_label}] {text}")
        if not self._own_bot_token:
            return [action]

        # Its own bot: the app's is another bot, which may not even be in this group.
        if self._own_bot is None:
            LOGGER.warning("%s: its bot isn't connected; a radio message wasn't relayed to Telegram", self.instance_name)
            return []
        await self._own_bot.bot.send_message(chat_id=action.chat_id, text=action.text)
        return []

    # --- Telegram -> radio ------------------------------------------------------------------------

    async def on_telegram_message(
        self,
        event: TelegramMessageEvent,
        context: PluginContext,
    ) -> list[PluginAction]:
        self._context = context
        if self._own_bot_token:
            return []  # Its group is read by its own bot (_on_own_bot_update).
        return await self._handle_telegram(event, context)

    async def on_telegram_button(
        self,
        event: TelegramButtonEvent,
        context: PluginContext,
    ) -> list[PluginAction]:
        self._context = context
        if not self._own_bot_token:
            await self._handle_button(event, context)
        return []

    async def _handle_button(self, event: TelegramButtonEvent, context: PluginContext) -> None:
        if self._desk is None or event.chat_id != self._group_id(context) or not event.data.startswith(sos_desk.PREFIX):
            return
        await self._desk.on_button(event)

    async def _handle_telegram(self, event: TelegramMessageEvent, context: PluginContext) -> list[PluginAction]:
        if self._desk is None:
            return self._relay_telegram(event, context)
        if event.chat_id != self._group_id(context) or event.is_from_bot:
            return []
        await self._desk.on_typed(event)
        return []

    def _relay_telegram(self, event: TelegramMessageEvent, context: PluginContext) -> list[PluginAction]:
        if event.chat_id != self._group_id(context):
            return []

        if event.is_from_bot:
            return []

        if not event.text:
            return []

        if event.text_source == "caption" and not context.settings.telegram.include_captions:
            return []

        text = event.text.strip()
        if not text:
            return []

        mesh_text = self._with_sender_prefix(context, event, text)

        chunking = context.settings.chunking
        payload_limit = self._payload_limit(context)
        min_split_payload_limit = utf8_len(chunking.prefix_template.format(index=1, total=1)) + 1
        payload_limit = max(min_split_payload_limit, payload_limit)

        if self.settings.get("long_messages") == LONG_TRUNCATE:
            chunks = [truncate_utf8(mesh_text, payload_limit)]
        else:
            try:
                chunks = split_for_mesh(
                    text=mesh_text,
                    payload_limit=payload_limit,
                    prefix_template=chunking.prefix_template,
                    chunking_enabled=chunking.enabled,
                )
            except ValueError as exc:
                if "Chunk prefix leaves no space for payload" not in str(exc):
                    raise

                LOGGER.warning(
                    "Chunk payload safety margin is too aggressive for this message; "
                    "falling back to full mesh payload limit"
                )
                chunks = split_for_mesh(
                    text=mesh_text,
                    payload_limit=context.mesh_payload_limit,
                    prefix_template=chunking.prefix_template,
                    chunking_enabled=chunking.enabled,
                )

        actions: list[PluginAction] = []
        bridge_channel = self._bridge_channel(context)
        is_chunked = len(chunks) > 1
        sequence_id = _chunk_sequence_id(event) if is_chunked else None
        configured_delay_ms = max(0, chunking.inter_chunk_delay_ms)
        effective_chunk_delay_ms = configured_delay_ms
        if is_chunked:
            effective_chunk_delay_ms = max(
                configured_delay_ms,
                self.MIN_CHUNK_DELAY_MS,
                max(0, chunking.broadcast_min_inter_chunk_delay_ms),
            )
            LOGGER.info(
                "Chunked Telegram message prepared: chat_id=%s message_id=%s sequence=%s chunks=%s payload_limit=%s delay_ms=%s",
                event.chat_id,
                event.message_id,
                sequence_id,
                len(chunks),
                payload_limit,
                effective_chunk_delay_ms,
            )
        for index, chunk in enumerate(chunks):
            delay_ms = effective_chunk_delay_ms if index > 0 else 0
            actions.append(
                SendMeshAction(
                    text=chunk,
                    channel_index=bridge_channel,
                    delay_ms=delay_ms,
                    retry_max_attempts=chunking.retry_max_attempts,
                    retry_initial_delay_ms=chunking.retry_initial_delay_ms,
                    retry_backoff_factor=chunking.retry_backoff_factor,
                    sequence_id=sequence_id,
                    sequence_index=(index + 1) if is_chunked else None,
                    sequence_total=len(chunks) if is_chunked else None,
                    abort_on_failure=chunking.abort_on_chunk_failure if is_chunked else False,
                )
            )

        return actions

    async def on_telegram_reaction(
        self,
        event: TelegramReactionEvent,
        context: PluginContext,
    ) -> list[PluginAction]:
        self._context = context
        if self._own_bot_token or self._sos_mode:
            return []  # The app's bot isn't this bridge's (its own relays no reactions); nor are they SOS traffic.

        if not self.settings.get("relay_reactions", True):
            LOGGER.info("Reaction to Telegram message %s not relayed: relay_reactions is off", event.message_id)
            return []

        group_id = self._group_id(context)
        if event.chat_id != group_id:
            LOGGER.info(
                "Reaction in Telegram chat %s not relayed by %s: its group is %s",
                event.chat_id,
                self.instance_name,
                group_id,
            )
            return []
        if event.is_from_bot or not event.emojis:
            return []

        # MeshCore has no reactions: a message names it, quoting the message reacted to,
        # which the reaction update itself doesn't carry (it comes from the app's cache).
        # A message Meshgram never saw the text of (a photo or sticker, or one from before
        # the cache) still gets its reaction relayed, just without the quote.
        emojis = "".join(event.emojis)
        quoted = " ".join((event.message_text or "").split())
        payload_limit = self._payload_limit(context)
        if not quoted:
            LOGGER.info(
                "Reaction to Telegram message %s relayed without a quote: Meshgram never saw its text "
                "(a message without text, or older than the last %s messages)",
                event.message_id,
                TELEGRAM_TEXT_CACHE_SIZE,
            )
            mesh_text = self._with_sender_prefix(context, event, f"reacted {emojis}")
        else:
            mesh_text = self._with_sender_prefix(context, event, f'reacted {emojis} to "{quoted}"')
        if quoted and utf8_len(mesh_text) > payload_limit:
            # One packet: shorten the quote rather than send a reaction in chunks.
            overflow = utf8_len(mesh_text) - payload_limit
            keep = utf8_len(quoted) - overflow - utf8_len(ELLIPSIS)
            shortened = _truncate_utf8(quoted, keep).rstrip()
            if shortened:
                mesh_text = self._with_sender_prefix(context, event, f'reacted {emojis} to "{shortened}{ELLIPSIS}"')
            if not shortened or utf8_len(mesh_text) > payload_limit:
                mesh_text = self._with_sender_prefix(context, event, f"reacted {emojis}")
        if utf8_len(mesh_text) > payload_limit:
            LOGGER.info("Reaction to Telegram message %s doesn't fit in a radio packet", event.message_id)
            return []

        LOGGER.info(
            "Relaying reaction to Telegram message %s to channel %s: %s",
            event.message_id,
            self._bridge_channel(context),
            mesh_text,
        )
        chunking = context.settings.chunking
        return [
            SendMeshAction(
                text=mesh_text,
                channel_index=self._bridge_channel(context),
                retry_max_attempts=chunking.retry_max_attempts,
                retry_initial_delay_ms=chunking.retry_initial_delay_ms,
                retry_backoff_factor=chunking.retry_backoff_factor,
            )
        ]

    def _radio_name(self, event: TelegramMessageEvent | TelegramReactionEvent, template: str) -> str:
        """The sender's name on the radio: their Telegram first name."""
        name = _compact_display_name(event.sender_display_name)
        if RELAY_MARK in template:
            # It must survive as "<name>": no brackets or colons, and no longer than a node's name.
            cleaned = "".join(char for char in name if char not in "<>:").strip()[:MAX_NODE_NAME_LENGTH].strip()
            return cleaned or "Telegram"
        return name


def truncate_utf8(text: str, max_bytes: int) -> str:
    """``text`` cut to ``max_bytes`` of UTF-8, ending in "…" when something was cut."""
    if utf8_len(text) <= max_bytes:
        return text
    return _truncate_utf8(text, max_bytes - utf8_len(ELLIPSIS)).rstrip() + ELLIPSIS


def _truncate_utf8(text: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    return text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


async def _shutdown_application(application: Any) -> None:
    """Stop a python-telegram-bot Application wherever its start got to."""
    if application is None:
        return
    updater = getattr(application, "updater", None)
    with contextlib.suppress(Exception):
        if updater is not None and updater.running:
            await updater.stop()
    with contextlib.suppress(Exception):
        if application.running:
            await application.stop()
    with contextlib.suppress(Exception):
        await application.shutdown()


def _compact_display_name(sender_display_name: str) -> str:
    normalized = " ".join(sender_display_name.split())
    if not normalized:
        return sender_display_name
    return normalized.split(" ", 1)[0]


def _chunk_sequence_id(event: TelegramMessageEvent) -> str:
    random_suffix = uuid.uuid4().hex[:8]
    return f"tg-{event.chat_id}-{event.message_id}-{random_suffix}"
