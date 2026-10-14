from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import time
from typing import AsyncIterator, Optional

from telegram import Message, Update
from telegram.error import NetworkError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from .config import (
    MIGRATION_HINT,
    ConfigError,
    MeshgramSettings,
    config_path_from_env,
    data_dir,
    legacy_env_vars,
    read_config,
)
from .core_settings import SETTINGS_FILE, CoreSettings, SettingsOverrideStore, check_telegram
from .firmware import FIRMWARE_FILE, FirmwareCatalog, FirmwareUpdater
from .plugin_manager import OVERRIDES_FILE, PluginManager, PluginOverrideStore
from .radio_admin import RadioAdmin
from .status import CONNECTED, CONNECTING, DISCONNECTED, StatusRegistry
from .transport import MeshCoreTransport, RadioCommandError
from .types import (
    MeshPacketRef,
    MeshTextEvent,
    PluginAction,
    PluginContext,
    SendMeshAction,
    SendTelegramAction,
    TelegramMessageEvent,
)
from .web import WebServer
from .web.api import register_control_api

LOGGER = logging.getLogger(__name__)
MESH_PACKET_ID_DEDUPE_TTL_SECONDS = 120.0
TELEGRAM_HEALTH_CHECK_SECONDS = 60.0
# Long enough for the control panel to get its answer before the web server goes.
RESTART_DELAY_SECONDS = 0.5


class MeshgramApp:
    def __init__(self, settings: MeshgramSettings, core: Optional[CoreSettings] = None):
        self.settings = settings
        self.core = core
        self.restart_requested = False
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.bot_app: Optional[Application] = None
        self.mesh = MeshCoreTransport(settings)
        self.status = StatusRegistry()
        self.status.set_state("radio", CONNECTING, self._mesh_description(), label="Radio")
        self.status.set_state("telegram", CONNECTING, "Starting bot", label="Telegram")
        self.web: Optional[WebServer] = WebServer(settings.web, self.status) if settings.web.enabled else None
        self.plugins = PluginManager(
            settings.plugins,
            host=self,
            store=PluginOverrideStore(data_dir() / OVERRIDES_FILE),
        )
        self.radio_admin = RadioAdmin(self.mesh)
        self.firmware = FirmwareUpdater(
            settings,
            self.mesh,
            self,
            catalog=FirmwareCatalog(data_dir() / "firmware"),
            store_path=data_dir() / FIRMWARE_FILE,
        )
        if self.web is not None:
            register_control_api(
                self.web,
                settings=settings,
                admin=self.radio_admin,
                plugins=self.plugins,
                send_mesh=self._send_mesh_from_web,
                core=core,
                restart=self.request_restart,
                firmware=self.firmware,
            )
        self._mesh_connect_task: Optional[asyncio.Task[None]] = None
        # Held while connecting, and while the radio is kept offline (firmware updates).
        self._radio_lock = asyncio.Lock()
        # Completed connections (plugins told included); the firmware updater waits for the next.
        self._radio_generation = 0
        self._radio_connected = asyncio.Condition()
        self._telegram_health_task: Optional[asyncio.Task[None]] = None
        self._seen_mesh_packet_ids: dict[MeshPacketRef, float] = {}
        self._mesh_send_lock = asyncio.Lock()

    async def _post_init(self, app: Application) -> None:
        self.loop = asyncio.get_running_loop()
        self._set_telegram_connected(app.bot.username)
        self._telegram_health_task = asyncio.create_task(self._monitor_telegram())

        await self._start_web()
        await self.plugins.start_all()
        # After the plugins started: they hear about the radio from on_mesh_connected.
        self._mesh_connect_task = asyncio.create_task(self._ensure_mesh_connected())
        LOGGER.info("Meshgram runtime initialized")

    async def _start_web(self) -> None:
        if self.web is None:
            LOGGER.info("Web app is off (web.enabled: false)")
            return
        try:
            await self.web.start()
        except OSError as exc:
            LOGGER.error(
                "Web app disabled: cannot listen on %s:%s (%s)", self.settings.web.host, self.settings.web.port, exc
            )
            self.web = None

    async def _ensure_mesh_connected(self) -> None:
        retry_delay_seconds = 5
        healthy_poll_seconds = 2

        while True:
            if self.mesh.is_connected:
                await asyncio.sleep(healthy_poll_seconds)
                continue

            async with self._radio_lock:
                if self.mesh.is_connected:
                    continue
                failed = await self._connect_mesh(retry_delay_seconds)
            if failed:
                await asyncio.sleep(retry_delay_seconds)

    async def _connect_mesh(self, retry_delay_seconds: int) -> bool:
        """One connection attempt; True if it failed."""
        if self.status.get("radio")["state"] == CONNECTED:
            self.status.set_state("radio", DISCONNECTED, f"Connection lost; reconnecting ({self._mesh_description()})")
        try:
            loop = asyncio.get_running_loop()
            await self.mesh.connect(loop, self._on_mesh_text)
            LOGGER.info("Radio connection established")
            self.status.set_state("radio", CONNECTED, self._mesh_description())
            await self.plugins.mesh_connected(self.mesh, self.plugin_context())
        except Exception as exc:
            self.mesh.invalidate_connection()
            self.status.set_state("radio", DISCONNECTED, f"{exc}; retrying in {retry_delay_seconds}s")
            LOGGER.warning(
                "Radio connection failed (%s). Retrying in %ss.",
                exc,
                retry_delay_seconds,
            )
            return True
        async with self._radio_connected:
            self._radio_generation += 1
            self._radio_connected.notify_all()
        return False

    # --- Radio maintenance (see meshgram.firmware.updater.RadioHost) ------------

    @property
    def radio_generation(self) -> int:
        return self._radio_generation

    @contextlib.asynccontextmanager
    async def radio_maintenance(self, reason: str) -> AsyncIterator[None]:
        """Disconnect the radio and keep it offline for the block; the connect loop takes over after."""
        async with self._radio_lock:
            LOGGER.info("Radio offline: %s", reason)
            self.status.set_state("radio", CONNECTING, reason)
            await self.mesh.disconnect()
            try:
                yield
            finally:
                self.status.set_state("radio", DISCONNECTED, f"Reconnecting ({self._mesh_description()})")

    async def wait_for_radio(self, after_generation: int, timeout: float) -> bool:
        try:
            async with self._radio_connected:
                await asyncio.wait_for(
                    self._radio_connected.wait_for(lambda: self._radio_generation > after_generation),
                    timeout,
                )
        except asyncio.TimeoutError:
            return False
        return self.mesh.is_connected

    async def _post_shutdown(self, app: Application) -> None:
        await self.firmware.stop()
        if self._telegram_health_task is not None:
            self._telegram_health_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._telegram_health_task
            self._telegram_health_task = None
        await self.plugins.stop_all()
        if self.web is not None:
            await self.web.stop()

    # --- Plugin host (see meshgram.plugin_manager.PluginHost) -----------------

    def plugin_context(self) -> PluginContext:
        self.mesh.refresh_local_node_id()
        return PluginContext(
            settings=self.settings,
            telegram_group_id=self.settings.telegram_group_id,
            mesh_payload_limit=self.mesh.payload_limit,
            local_node_id=self.mesh.local_node_id,
            status=self.status,
            web=self.web,
            execute_actions=self.execute_actions,
        )

    def connected_transport(self) -> Optional[MeshCoreTransport]:
        return self.mesh if self.mesh.is_connected else None

    async def execute_actions(self, actions: list[PluginAction], plugin_name: str) -> None:
        aborted_sequences: set[str] = set()
        for action in actions:
            if isinstance(action, SendMeshAction):
                sequence_id = action.sequence_id
                if (
                    sequence_id
                    and action.abort_on_failure
                    and sequence_id in aborted_sequences
                ):
                    LOGGER.warning(
                        "Skipping mesh chunk due to prior sequence failure: "
                        "sequence=%s chunk=%s/%s plugin=%s",
                        sequence_id,
                        action.sequence_index,
                        action.sequence_total,
                        plugin_name,
                    )
                    continue

            try:
                if isinstance(action, SendTelegramAction):
                    await self._execute_send_telegram(action)
                elif isinstance(action, SendMeshAction):
                    await self._execute_send_mesh(action)
                else:
                    LOGGER.warning("Plugin %s returned unknown action type: %s", plugin_name, type(action))
            except Exception:
                if (
                    isinstance(action, SendMeshAction)
                    and action.sequence_id
                    and action.abort_on_failure
                ):
                    aborted_sequences.add(action.sequence_id)
                LOGGER.exception("Plugin %s failed executing action %s", plugin_name, type(action).__name__)

    # --- Connection status ----------------------------------------------------

    def _mesh_description(self) -> str:
        connection = self.settings.meshcore.connection
        if connection.mode == "tcp":
            target = f"{connection.tcp_host}:{connection.tcp_port}"
        elif connection.mode == "ble":
            target = connection.ble_address or ""
        else:
            target = connection.serial_device or "auto-detect"
        return " ".join(part for part in ("meshcore", connection.mode, target) if part)

    def _set_telegram_connected(self, username: Optional[str]) -> None:
        self.status.set_state("telegram", CONNECTED, f"@{username}" if username else "Bot API reachable")

    async def _monitor_telegram(self) -> None:
        # Polling errors mark the bot disconnected (_on_telegram_error); this notices recovery.
        while True:
            await asyncio.sleep(TELEGRAM_HEALTH_CHECK_SECONDS)
            if self.bot_app is None:
                continue
            try:
                me = await self.bot_app.bot.get_me()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status.set_state("telegram", DISCONNECTED, f"Bot API unreachable: {exc}")
                continue
            self._set_telegram_connected(me.username)

    async def _on_telegram_update(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Receiving an update proves polling works again after an outage.
        self._set_telegram_connected(context.bot.username)

    async def _on_telegram_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        error = context.error
        if isinstance(error, NetworkError):
            if update is None:
                # Raised while fetching updates: the bot can't reach Telegram right now.
                self.status.set_state("telegram", DISCONNECTED, f"Polling failed: {error}")
            LOGGER.warning("Telegram network error: %s", error)
            return
        LOGGER.error("Unhandled error while processing Telegram update", exc_info=error)

    # --- Telegram inbound -----------------------------------------------------

    async def _handle_telegram_message(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        message = update.effective_message
        if not message:
            return

        chat = message.chat
        if not chat:
            return

        text: Optional[str] = None
        text_source: Optional[str] = None

        if message.text is not None:
            text = message.text
            text_source = "text"
        elif message.caption is not None:
            text = message.caption
            text_source = "caption"

        from_user = message.from_user
        sender_display_name = "Unknown"
        is_from_bot = False
        if from_user is not None:
            sender_display_name = (
                from_user.full_name or from_user.username or str(from_user.id)
            )
            is_from_bot = bool(from_user.is_bot)

        event = TelegramMessageEvent(
            chat_id=chat.id,
            message_id=message.message_id,
            text=text,
            text_source=text_source,
            is_from_bot=is_from_bot,
            sender_display_name=sender_display_name,
            has_media=_message_has_media(message),
            raw_message=message,
        )

        await self._dispatch_telegram_message(event)

    async def _dispatch_telegram_message(self, event: TelegramMessageEvent) -> None:
        context = self.plugin_context()

        for name, plugin in self.plugins.running:
            try:
                actions = await plugin.on_telegram_message(event, context)
            except Exception:
                LOGGER.exception("Plugin %s failed handling telegram message", name)
                continue

            await self.execute_actions(actions, name)

    # --- Mesh inbound ---------------------------------------------------------

    async def _on_mesh_text(self, event: MeshTextEvent) -> None:
        if event.packet_id is not None and self._is_duplicate_mesh_packet_id(event.packet_id):
            return
        await self._dispatch_mesh_message(event)

    def _is_duplicate_mesh_packet_id(self, packet_id: MeshPacketRef) -> bool:
        now = time.monotonic()
        expiry_cutoff = now - MESH_PACKET_ID_DEDUPE_TTL_SECONDS

        stale_packet_ids = [
            seen_packet_id
            for seen_packet_id, seen_time in self._seen_mesh_packet_ids.items()
            if seen_time < expiry_cutoff
        ]
        for stale_packet_id in stale_packet_ids:
            self._seen_mesh_packet_ids.pop(stale_packet_id, None)

        if packet_id in self._seen_mesh_packet_ids:
            return True

        self._seen_mesh_packet_ids[packet_id] = now
        return False

    async def _dispatch_mesh_message(self, event: MeshTextEvent) -> None:
        context = self.plugin_context()

        for name, plugin in self.plugins.running:
            try:
                actions = await plugin.on_mesh_message(event, context)
            except Exception:
                LOGGER.exception("Plugin %s failed handling mesh message", name)
                continue

            await self.execute_actions(actions, name)

    # --- Action execution -----------------------------------------------------

    async def _execute_send_telegram(self, action: SendTelegramAction):
        if self.bot_app is None:
            raise RuntimeError("Telegram bot app is not initialized")

        return await self.bot_app.bot.send_message(chat_id=action.chat_id, text=action.text)

    async def _execute_send_mesh(self, action: SendMeshAction) -> Optional[MeshPacketRef]:
        async with self._mesh_send_lock:
            if action.delay_ms > 0:
                await asyncio.sleep(action.delay_ms / 1000)

            max_attempts = max(1, action.retry_max_attempts)
            retry_delay_seconds = max(0, action.retry_initial_delay_ms) / 1000
            backoff_factor = max(1.0, action.retry_backoff_factor)

            for attempt in range(1, max_attempts + 1):
                try:
                    if not self.mesh.is_connected:
                        if max_attempts == 1:
                            LOGGER.warning("Mesh transport is not connected yet; dropping outbound message")
                            return None
                        raise RuntimeError("Mesh transport is not connected yet")

                    packet_id = await self.mesh.asend_text(action)
                    if action.sequence_id is not None:
                        LOGGER.info(
                            "Mesh chunk sent: sequence=%s chunk=%s/%s packet_id=%s bytes=%s",
                            action.sequence_id,
                            action.sequence_index,
                            action.sequence_total,
                            packet_id,
                            len(action.text.encode("utf-8")),
                        )
                    return packet_id
                except Exception as exc:
                    if _is_connection_error(exc):
                        self.mesh.invalidate_connection()

                    if attempt >= max_attempts:
                        LOGGER.error(
                            "Mesh send exhausted retries: sequence=%s chunk=%s/%s "
                            "attempts=%s abort_on_failure=%s",
                            action.sequence_id,
                            action.sequence_index,
                            action.sequence_total,
                            max_attempts,
                            action.abort_on_failure,
                            exc_info=True,
                        )
                        raise

                    LOGGER.warning(
                        "Mesh send failed; retrying in %.2fs "
                        "(attempt %s/%s, sequence=%s, chunk=%s/%s)",
                        retry_delay_seconds,
                        attempt + 1,
                        max_attempts,
                        action.sequence_id,
                        action.sequence_index,
                        action.sequence_total,
                    )
                    if retry_delay_seconds > 0:
                        await asyncio.sleep(retry_delay_seconds)
                    retry_delay_seconds *= backoff_factor

            return None

    async def _send_mesh_from_web(self, action: SendMeshAction) -> MeshPacketRef:
        packet_id = await self._execute_send_mesh(action)
        if packet_id is None:
            raise RadioCommandError("The radio isn't connected")
        return packet_id

    # --- Lifecycle ------------------------------------------------------------

    def request_restart(self) -> None:
        """Stop, then start again from the top (``main``), reading the settings anew."""
        if self.bot_app is None:
            raise RuntimeError("Meshgram isn't running")
        if self.firmware.busy:
            raise RuntimeError("The radio's firmware is being updated; restart when it's done")
        LOGGER.info("Restarting Meshgram, as asked from the web app")
        self.restart_requested = True
        asyncio.get_running_loop().call_later(RESTART_DELAY_SECONDS, self.bot_app.stop_running)

    def run(self) -> None:
        self.bot_app = (
            ApplicationBuilder()
            .token(self.settings.telegram_bot_token)
            .post_init(self._post_init)
            .post_shutdown(self._post_shutdown)
            .build()
        )

        self.bot_app.add_handler(
            MessageHandler(filters.ALL & ~filters.COMMAND, self._handle_telegram_message)
        )
        self.bot_app.add_handler(TypeHandler(Update, self._on_telegram_update), group=-1)
        self.bot_app.add_error_handler(self._on_telegram_error)

        LOGGER.info("Starting Meshgram polling loop")
        try:
            self.bot_app.run_polling(allowed_updates=Update.ALL_TYPES)
        finally:
            if self._mesh_connect_task is not None:
                self._mesh_connect_task.cancel()
            self.mesh.close()


# ---------------------------------------------------------------------------
# Telegram-side helpers

def _message_has_media(message: Message) -> bool:
    media_fields = (
        "animation",
        "audio",
        "document",
        "photo",
        "sticker",
        "video",
        "video_note",
        "voice",
    )

    for field in media_fields:
        value = getattr(message, field, None)
        if value:
            return True
    return False


def _is_connection_error(exc: Exception) -> bool:
    return isinstance(exc, (ConnectionError, TimeoutError, OSError, EOFError))


def main() -> None:
    # INFO until the settings say otherwise, so what loading them logs is seen.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s", force=True)
    config_path = config_path_from_env()
    try:
        core = CoreSettings(
            read_config(config_path),
            config_path=config_path,
            store=SettingsOverrideStore(data_dir() / SETTINGS_FILE),
            check_telegram=check_telegram,
        )
    except ConfigError as exc:
        raise SystemExit(f"meshgram: {exc}") from None
    settings = core.settings
    logging.getLogger().setLevel(getattr(logging, settings.log_level, logging.INFO))

    ignored = legacy_env_vars()
    if ignored:
        LOGGER.warning("Ignoring environment variables %s. %s.", ", ".join(ignored), MIGRATION_HINT)

    app = MeshgramApp(settings, core)
    app.run()
    if app.restart_requested:
        logging.shutdown()
        # The same command again, in this process (so Docker and service managers see no exit).
        os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])
