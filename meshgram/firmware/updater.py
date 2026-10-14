"""Firmware updates for the radio, from the control panel.

``FirmwareUpdater.describe`` says what's installed, what's published for the
radio's board and whether it can be updated here; ``start`` runs an update in
the background:

1. download the release file (cached) and check it against GitHub's size and
   SHA-256, then check it's an update for this kind of board (an ESP32 app
   image for the right chip, an application-only nRF52 DFU package);
2. pause the radio: the app stops reconnecting, the link is closed so the
   serial port is free (``RadioHost.radio_maintenance``);
3. flash in a separate process (``worker``): it puts the radio into its
   bootloader itself and writes the update without erasing anything (settings,
   identity, contacts and channels stay); an ESP32 transfer that fails is tried
   again at a slower speed;
4. let the app reconnect, which runs every plugin's ``on_mesh_connected`` as
   after any reconnect; an ESP32 radio that doesn't come back is reset once;
5. check the radio now reports the new version, and the same identity.

Progress is published as ``{"type": "firmware", "job": ...}`` events (and the
``firmware`` snapshot key) for open pages.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, AsyncContextManager, Awaitable, Callable, Optional, Protocol

from ..config import MeshgramSettings
from ..radio_admin import RadioAdminError
from ..storage import write_private_json
from .catalog import (
    PLATFORM_ESP32,
    PLATFORM_NRF52,
    PLATFORM_TITLES,
    ROLE_USB,
    SUPPORTED_PLATFORMS,
    UNSUPPORTED_PLATFORM_HINTS,
    FirmwareAsset,
    FirmwareCatalog,
    FirmwareCatalogError,
    FirmwareRelease,
    board_platform,
    boards_for_model,
    format_version,
    parse_version,
    update_file,
)
from .images import FirmwareImageError, check_esp_image, check_nrf_package

LOGGER = logging.getLogger(__name__)

FIRMWARE_FILE = "firmware.json"
FLASH_TIMEOUT_SECONDS = 10 * 60
RECONNECT_TIMEOUT_SECONDS = 90.0
# After the reset an ESP32 radio gets when it didn't come back by itself.
RETRY_RECONNECT_TIMEOUT_SECONDS = 60.0
PORT_WAIT_SECONDS = 10.0
ESP_FLASH_BAUD = 460800
ESP_SAFE_BAUD = 115200
LOG_LINES = 60

RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"


class FirmwareError(RadioAdminError):
    """An update that can't be started as asked."""


class FirmwareConflictError(FirmwareError):
    status = 409


class FirmwareNotFoundError(FirmwareError):
    status = 404


class FirmwareUnavailableError(FirmwareError):
    status = 502


class _JobFailed(Exception):
    pass


class RadioHost(Protocol):
    """What the updater needs from the app (``MeshgramApp``)."""

    @property
    def radio_generation(self) -> int:
        """Counts completed radio connections (plugins told included)."""

    def radio_maintenance(self, reason: str) -> AsyncContextManager[None]:
        """Disconnect the radio and keep it disconnected for the block."""

    async def wait_for_radio(self, after_generation: int, timeout: float) -> bool:
        """Wait for a connection newer than ``after_generation``."""


OnEvent = Callable[[dict[str, Any]], None]
WorkerRunner = Callable[[dict[str, Any], OnEvent, float], Awaitable[dict[str, Any]]]


async def run_worker(spec: dict[str, Any], on_event: OnEvent, timeout: float) -> dict[str, Any]:
    """Run ``meshgram.firmware.worker`` with ``spec``; its ``result`` event (``ok``, ``error``)."""
    root = str(Path(__file__).resolve().parents[2])
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(part for part in (root, env.get("PYTHONPATH")) if part)
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "meshgram.firmware.worker",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=root,
        env=env,
    )  # fmt: skip
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    process.stdin.write(json.dumps(spec).encode("utf-8"))
    await process.stdin.drain()
    process.stdin.close()
    result: dict[str, Any] = {}
    errors: collections.deque[str] = collections.deque(maxlen=10)

    async def read_events() -> None:
        async for line in process.stdout:  # type: ignore[union-attr]
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("event") == "result":
                result.update(event)
            else:
                on_event(event)

    async def read_errors() -> None:
        async for line in process.stderr:  # type: ignore[union-attr]
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                errors.append(text)
                LOGGER.debug("Firmware worker: %s", text)

    try:
        await asyncio.wait_for(asyncio.gather(read_events(), read_errors(), process.wait()), timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        return {"ok": False, "error": f"Flashing didn't finish within {int(timeout // 60)} minutes and was stopped"}
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        raise
    if "ok" not in result:
        detail = f": {errors[-1]}" if errors else ""
        return {"ok": False, "error": f"The flashing tool stopped unexpectedly (exit code {process.returncode}){detail}"}
    return result


class FirmwareUpdater:
    def __init__(
        self,
        settings: MeshgramSettings,
        transport: Any,
        host: RadioHost,
        *,
        catalog: FirmwareCatalog,
        store_path: Path,
        runner: WorkerRunner = run_worker,
    ):
        self.settings = settings
        self.transport = transport
        self.host = host
        self.catalog = catalog
        self.store_path = store_path
        self.runner = runner
        # Called with every job change (the app publishes it to open pages).
        self.on_change: Optional[Callable[[dict[str, Any]], None]] = None
        self.job: Optional[dict[str, Any]] = None
        self._task: Optional[asyncio.Task[None]] = None

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    # --- What's installed and available ----------------------------------------------------

    @property
    def serial_port(self) -> Optional[str]:
        port = getattr(self.transport, "serial_port", None) or self.settings.meshcore.connection.serial_device
        return str(port) if port else None

    def connection_problem(self) -> Optional[str]:
        connection = self.settings.meshcore.connection
        if connection.mode != "serial":
            return (
                f"Firmware updates need the radio on USB: Meshgram reaches it over {connection.mode.upper()} "
                "(meshcore.connection.mode). Update it with the MeshCore web flasher instead"
            )
        if not self.serial_port:
            return "Set the radio's serial device (meshcore.connection.serial_device) to update its firmware"
        return None

    def _saved_boards(self) -> dict[str, str]:
        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        boards = data.get("boards") if isinstance(data, dict) else None
        return {str(key): str(value) for key, value in boards.items()} if isinstance(boards, dict) else {}

    def _save_board(self, model: Optional[str], board: str) -> None:
        if not model:
            return
        boards = self._saved_boards()
        if boards.get(model) == board:
            return
        boards[model] = board
        try:
            write_private_json(self.store_path, {"boards": boards})
        except OSError as exc:
            LOGGER.warning("Couldn't remember the radio's board: %s", exc)

    @staticmethod
    def _boards(releases: list[FirmwareRelease]) -> dict[str, str]:
        """Every board with companion USB firmware, and its platform (from its newest release)."""
        boards: dict[str, str] = {}
        for release in releases:
            files: dict[str, list[FirmwareAsset]] = collections.defaultdict(list)
            for asset in release.assets:
                if asset.role == ROLE_USB:
                    files[asset.board].append(asset)
            for board, assets in files.items():
                platform = board_platform(assets)
                if board not in boards and platform is not None:
                    boards[board] = platform
        return boards

    def _device(self) -> dict[str, Any]:
        connected = bool(self.transport.is_connected)
        info = self.transport.device_info if connected else {}
        identity = self.transport.device_self_info if connected else {}
        version = parse_version(info.get("ver"))
        return {
            "connected": connected,
            "model": info.get("model") or None,
            "version": info.get("ver") or None,
            "version_number": format_version(version) if version else None,
            "build": info.get("fw_build") or None,
            "public_key": identity.get("public_key") or None,
        }

    async def describe(self, *, board: Optional[str] = None, refresh: bool = False) -> dict[str, Any]:
        state = await self.catalog.releases(refresh=refresh)
        device = self._device()
        boards = self._boards(state.releases)
        detected = boards_for_model(device["model"], set(boards))
        saved = self._saved_boards().get(device["model"] or "")
        if board is not None and board not in boards:
            raise FirmwareNotFoundError(f"There's no companion firmware for a board called {board}")
        if board is not None:
            selected, source = board, "chosen"
        elif saved in boards and (not detected or saved in detected):
            selected, source = saved, "saved"
        elif len(detected) == 1:
            selected, source = detected[0], "detected"
        else:
            selected, source = None, None
        platform = boards.get(selected) if selected else None

        installed = parse_version(device["version"])
        releases = []
        for release in state.releases:
            asset = update_file(release.board_assets(selected), platform) if selected else None
            if asset is None:
                continue
            releases.append({
                "version": release.version,
                "tag": release.tag,
                "name": release.name,
                "published_at": release.published_at,
                "prerelease": release.prerelease,
                "notes_url": release.notes_url,
                "file": {"name": asset.name, "size": asset.size, "verified": asset.sha256 is not None},
                "installed": installed is not None and release.version_tuple == installed,
                "newer": installed is not None and release.version_tuple > installed,
            })
        latest = next((release for release in releases if not release["prerelease"]), None)

        problems = []
        connection_problem = self.connection_problem()
        if connection_problem:
            problems.append(connection_problem)
        if platform is not None and platform not in SUPPORTED_PLATFORMS:
            problems.append(UNSUPPORTED_PLATFORM_HINTS.get(platform, f"{PLATFORM_TITLES.get(platform, platform)} boards can't be updated here"))
        if not state.releases:
            problems.append(state.error or "No firmware releases found")

        return {
            "device": device,
            "connection": {"mode": self.settings.meshcore.connection.mode, "port": self.serial_port},
            "boards": [
                {"name": name, "platform": platform_name, "detected": name in detected}
                for name, platform_name in sorted(boards.items(), key=lambda item: item[0].lower())
            ],
            "detected_boards": detected,
            "board": selected,
            "board_source": source,
            "platform": platform,
            "platform_title": PLATFORM_TITLES.get(platform or ""),
            "releases": releases,
            "latest": latest["version"] if latest else None,
            "update_available": bool(latest and latest["newer"]),
            "can_update": not problems,
            "problems": problems,
            "catalog": {
                "error": state.error,
                "age_seconds": round(time.monotonic() - state.fetched_at) if state.fetched_at is not None else None,
            },
            "job": self.public_job(),
        }

    # --- Updating --------------------------------------------------------------------------------

    async def start(self, *, version: Any, board: Any, allow_downgrade: Any = False) -> dict[str, Any]:
        """Check the request and start the update; returns the job (progress follows as events)."""
        if self.busy:
            raise FirmwareConflictError("An update is already running")
        if not isinstance(version, str) or parse_version(version) is None:
            raise FirmwareError("Pick the version to install")
        if not isinstance(board, str) or not board:
            raise FirmwareError("Pick the radio's board")
        if not isinstance(allow_downgrade, bool):
            raise FirmwareError("allow_downgrade must be true or false")
        problem = self.connection_problem()
        if problem:
            raise FirmwareConflictError(problem)

        state = await self.catalog.releases()
        if not state.releases:
            raise FirmwareUnavailableError(state.error or "No firmware releases found")
        boards = self._boards(state.releases)
        platform = boards.get(board)
        if platform is None:
            raise FirmwareNotFoundError(f"There's no companion firmware for a board called {board}")
        if platform not in SUPPORTED_PLATFORMS:
            raise FirmwareConflictError(UNSUPPORTED_PLATFORM_HINTS.get(platform, "This board can't be updated here"))
        wanted = parse_version(version)
        release = next((item for item in state.releases if item.version_tuple == wanted), None)
        asset = update_file(release.board_assets(board), platform) if release else None
        if release is None or asset is None:
            raise FirmwareNotFoundError(f"Version {version} has no companion firmware for {board}")

        device = self._device()
        if device["connected"]:
            detected = boards_for_model(device["model"], set(boards))
            if detected and board not in detected:
                raise FirmwareConflictError(
                    f"The radio says it's a {device['model']}, which runs {' or '.join(detected)} firmware, not {board}"
                )
            installed = parse_version(device["version"])
            if installed is not None and release.version_tuple < installed and not allow_downgrade:
                raise FirmwareConflictError(
                    f"Version {release.version} is older than the installed {format_version(installed)}; confirm the downgrade"
                )

        self.job = {
            "id": uuid.uuid4().hex[:12],
            "state": RUNNING,
            "stage": "download",
            "text": "Downloading the firmware",
            "percent": None,
            "board": board,
            "platform": platform,
            "version": release.version,
            "from_version": device["version"],
            "model": device["model"],
            "file": asset.name,
            "started_at": time.time(),
            "finished_at": None,
            "error": None,
            "warnings": [],
            "log": [],
        }
        LOGGER.warning(
            "Firmware update started from the web app: %s %s -> %s (%s)",
            device["model"] or board, device["version"] or "unknown version", release.version, asset.name,
        )  # fmt: skip
        self._changed()
        self._task = asyncio.create_task(self._run(release, asset, platform, device))
        return self.public_job()

    def public_job(self) -> Optional[dict[str, Any]]:
        if self.job is None:
            return None
        return {**self.job, "log": list(self.job["log"]), "warnings": list(self.job["warnings"])}

    def snapshot(self) -> dict[str, Any]:
        return {"firmware": self.public_job()}

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    def _changed(self) -> None:
        if self.on_change is not None and self.job is not None:
            try:
                self.on_change(self.public_job() or {})
            except Exception:
                LOGGER.exception("Firmware update: publishing progress failed")

    def _stage(self, stage: str, text: str, percent: Optional[int] = None) -> None:
        assert self.job is not None
        self.job.update(stage=stage, text=text, percent=percent)
        self._log(text)

    def _log(self, text: str) -> None:
        assert self.job is not None
        stamp = time.strftime("%H:%M:%S")
        self.job["log"] = [*self.job["log"], f"{stamp} {text}"][-LOG_LINES:]
        LOGGER.info("Firmware update: %s", text)
        self._changed()

    def _on_worker_event(self, event: dict[str, Any]) -> None:
        assert self.job is not None
        kind = event.get("event")
        if kind == "progress" and isinstance(event.get("percent"), (int, float)):
            percent = max(0, min(100, int(event["percent"])))
            if percent != self.job["percent"]:
                self.job["percent"] = percent
                self._changed()
        elif kind == "stage":
            self._stage("flash", str(event.get("text") or "Flashing"), self.job["percent"] if event.get("stage") == "restarting" else None)
            self.job["flash_stage"] = event.get("stage")
        elif kind == "log" and event.get("text"):
            self._log(str(event["text"])[:300])

    def _on_reset_event(self, event: dict[str, Any]) -> None:
        if event.get("event") == "log" and event.get("text"):
            self._log(str(event["text"])[:300])

    async def _run(self, release: FirmwareRelease, asset: FirmwareAsset, platform: str, before: dict[str, Any]) -> None:
        assert self.job is not None
        job = self.job
        try:
            self._stage("download", f"Downloading {asset.name}")
            try:
                path = await self.catalog.download(release, asset)
            except FirmwareCatalogError as exc:
                raise _JobFailed(str(exc)) from None
            self._log("Checksum verified" if asset.sha256 else "GitHub lists no checksum for this file; its size matches")
            if asset.sha256 is None:
                job["warnings"].append("GitHub listed no checksum for this file; only its size was checked")

            self._stage("verify", "Checking the firmware file")
            try:
                if platform == PLATFORM_ESP32:
                    image = check_esp_image(await asyncio.to_thread(path.read_bytes))
                    self._log(f"ESP32 application image for the {image.chip_name}, {image.size} bytes")
                else:
                    package = await asyncio.to_thread(check_nrf_package, str(path))
                    self._log(f"nRF52 application DFU package, {package.application_size} bytes")
            except FirmwareImageError as exc:
                raise _JobFailed(str(exc)) from None
            await asyncio.to_thread(self.catalog.prune, path)

            generation = self.host.radio_generation
            port = self.serial_port
            assert port is not None
            self._stage("disconnect", "Disconnecting from the radio")
            async with self.host.radio_maintenance(f"Updating the firmware to {release.version}"):
                await self._wait_for_port(port)
                self._stage("flash", "Starting the flashing tool", 0)
                spec = {"action": "flash", "platform": platform, "port": port, "file": str(path)}
                if platform == PLATFORM_ESP32:
                    spec["baud"] = ESP_FLASH_BAUD
                result = await self.runner(spec, self._on_worker_event, FLASH_TIMEOUT_SECONDS)
                if not result.get("ok") and platform == PLATFORM_ESP32 and job.get("flash_stage") == "writing":
                    # USB serial chips that can't keep up at the higher speed: once more, slowly.
                    self._log(f"Writing failed ({result.get('error')}); trying again at {ESP_SAFE_BAUD} baud")
                    job.pop("flash_stage", None)
                    result = await self.runner({**spec, "baud": ESP_SAFE_BAUD}, self._on_worker_event, FLASH_TIMEOUT_SECONDS)
                if not result.get("ok"):
                    raise _JobFailed(str(result.get("error") or "Flashing failed"))
                self._log("Firmware written")

            self._stage("reconnect", "Waiting for the radio to restart and reconnect")
            connected = await self.host.wait_for_radio(generation, RECONNECT_TIMEOUT_SECONDS)
            if not connected and platform == PLATFORM_ESP32:
                self._log("The radio didn't come back; resetting it")
                generation = self.host.radio_generation
                async with self.host.radio_maintenance("Resetting the radio after the firmware update"):
                    await self._wait_for_port(port)
                    reset = await self.runner({"action": "reset", "platform": platform, "port": port}, self._on_reset_event, 120)
                    if not reset.get("ok"):
                        self._log(f"Resetting failed: {reset.get('error')}")
                connected = await self.host.wait_for_radio(generation, RETRY_RECONNECT_TIMEOUT_SECONDS)
            if not connected:
                raise _JobFailed(
                    "The firmware was written, but the radio hasn't reconnected yet. Meshgram keeps trying; "
                    "if it doesn't come back, unplug the radio and plug it in again"
                )

            self._stage("check", "Checking the installed version")
            after = self._device()
            installed = parse_version(after["version"])
            if installed != release.version_tuple:
                raise _JobFailed(f"The radio restarted but reports version {after['version'] or 'unknown'}, not {release.version}")
            if before.get("public_key") and after["public_key"] and before["public_key"] != after["public_key"]:
                job["warnings"].append("The radio has a new identity (public key): its settings were reset")
            self._save_board(after["model"] or before.get("model"), job["board"])
            job.update(state=SUCCEEDED, stage="done", text=f"Version {release.version} is installed", percent=100, finished_at=time.time())
            self._log(job["text"])
            LOGGER.warning("Firmware update finished: the radio runs %s", after["version"])
        except _JobFailed as exc:
            self._fail(str(exc))
        except asyncio.CancelledError:
            self._fail("The update was interrupted (Meshgram is stopping)")
            raise
        except Exception as exc:
            LOGGER.exception("Firmware update failed")
            self._fail(f"Unexpected error: {exc}")

    def _fail(self, message: str) -> None:
        assert self.job is not None
        self.job.update(state=FAILED, error=message, finished_at=time.time())
        LOGGER.error("Firmware update failed: %s", message)
        self._log(f"Failed: {message}")

    async def _wait_for_port(self, port: str) -> None:
        deadline = time.monotonic() + PORT_WAIT_SECONDS
        while not await asyncio.to_thread(os.path.exists, port):
            if time.monotonic() >= deadline:
                raise _JobFailed(f"The radio's serial port {port} isn't there; check its USB connection")
            await asyncio.sleep(0.5)
