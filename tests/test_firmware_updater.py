import asyncio
import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from meshgram.config import MeshgramSettings
from meshgram.firmware import updater as updater_module
from meshgram.firmware.catalog import FirmwareCatalog
from meshgram.firmware.updater import (
    FAILED,
    SUCCEEDED,
    FirmwareConflictError,
    FirmwareError,
    FirmwareNotFoundError,
    FirmwareUpdater,
    run_worker,
)

try:
    from test_firmware_catalog import BOARD_FILES, esp_image, release
except ImportError:
    from tests.test_firmware_catalog import BOARD_FILES, esp_image, release

ESP_IMAGE = esp_image(chip_id=9)
KEY = "ab" * 32


class FakeTransport:
    def __init__(self):
        self.is_connected = True
        self.device_info = {"model": "Heltec V3", "ver": "v1.16.0", "fw_build": "01 Jul 2026"}
        self.device_self_info = {"public_key": KEY}
        self.serial_port = None


class FakeHost:
    """Stands in for MeshgramApp: radio maintenance and reconnects."""

    def __init__(self, transport, after_flash="v1.17.1"):
        self.transport = transport
        self.radio_generation = 0
        self.maintenance = []
        self.after_flash = after_flash
        self.reconnects = True

    @contextlib.asynccontextmanager
    async def radio_maintenance(self, reason):
        self.maintenance.append(reason)
        self.transport.is_connected = False
        yield

    async def wait_for_radio(self, after_generation, timeout):
        if not self.reconnects:
            return False
        self.radio_generation += 1
        self.transport.is_connected = True
        self.transport.device_info = {**self.transport.device_info, "ver": self.after_flash}
        return True


class FakeRunner:
    def __init__(self, results=None):
        self.specs = []
        self.results = list(results or [])
        self.stages = ["bootloader", "writing"]

    async def __call__(self, spec, on_event, timeout):
        self.specs.append(spec)
        for stage in self.stages:
            on_event({"event": "stage", "stage": stage, "text": stage})
        on_event({"event": "progress", "percent": 50})
        on_event({"event": "log", "text": "Hash of data verified."})
        return self.results.pop(0) if self.results else {"ok": True}


class UpdaterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.dir = Path(tempdir.name)
        self.port = self.dir / "ttyUSB0"
        self.port.write_text("")
        self.settings = MeshgramSettings(telegram_bot_token="t", telegram_group_id=-1, config_path="config.yaml")
        self.settings.meshcore.connection.serial_device = str(self.port)
        raw = [release(version, BOARD_FILES, data=ESP_IMAGE) for version in ("1.17.1", "1.16.0", "1.15.0")]
        self.catalog = FirmwareCatalog(
            self.dir / "cache",
            fetch=lambda url: json.dumps(raw).encode(),
            download=lambda url, target: target.write_bytes(ESP_IMAGE),
        )
        self.transport = FakeTransport()
        self.host = FakeHost(self.transport)
        self.runner = FakeRunner()
        self.events = []
        self.updater = self.make_updater()

    def make_updater(self):
        updater = FirmwareUpdater(
            self.settings, self.transport, self.host,
            catalog=self.catalog, store_path=self.dir / "firmware.json", runner=self.runner,
        )  # fmt: skip
        updater.on_change = self.events.append
        return updater

    async def finish(self):
        await self.updater._task
        return self.updater.job

    async def test_describe_picks_the_board_and_finds_the_update(self):
        info = await self.updater.describe()
        self.assertEqual(info["detected_boards"], ["Heltec_v3", "Heltec_WSL3"])
        # Two variants: the user has to say which.
        self.assertIsNone(info["board"])
        self.assertEqual(info["releases"], [])

        info = await self.updater.describe(board="Heltec_v3")
        self.assertEqual((info["board"], info["board_source"], info["platform"]), ("Heltec_v3", "chosen", "esp32"))
        self.assertEqual([r["version"] for r in info["releases"]], ["1.17.1", "1.16.0", "1.15.0"])
        self.assertEqual(info["latest"], "1.17.1")
        self.assertTrue(info["update_available"])
        self.assertTrue(info["can_update"])
        self.assertTrue(info["releases"][1]["installed"])
        with self.assertRaises(FirmwareNotFoundError):
            await self.updater.describe(board="Nope")

    async def test_a_single_matching_board_is_picked(self):
        self.transport.device_info = {"model": "RAK 4631", "ver": "1.17.1"}
        info = await self.updater.describe()
        self.assertEqual((info["board"], info["board_source"], info["platform"]), ("RAK_4631", "detected", "nrf52"))
        self.assertFalse(info["update_available"])

    async def test_update_flashes_reconnects_and_checks(self):
        job = await self.updater.start(version="1.17.1", board="Heltec_v3")
        self.assertEqual(job["state"], "running")
        with self.assertRaises(FirmwareConflictError):
            await self.updater.start(version="1.17.1", board="Heltec_v3")
        job = await self.finish()
        self.assertEqual(job["state"], SUCCEEDED, job)
        self.assertEqual(job["percent"], 100)
        self.assertEqual(job["warnings"], [])
        spec = self.runner.specs[0]
        self.assertEqual((spec["action"], spec["platform"], spec["port"], spec["baud"]), ("flash", "esp32", str(self.port), 460800))
        self.assertTrue(Path(spec["file"]).read_bytes().startswith(b"\xe9"))
        self.assertEqual(len(self.host.maintenance), 1)
        self.assertIn("running", [event["state"] for event in self.events])
        self.assertEqual(self.events[-1]["state"], SUCCEEDED)
        # The board is remembered for this model.
        self.assertEqual((await self.make_updater().describe())["board"], "Heltec_v3")

    async def test_failed_flash(self):
        self.runner.results = [{"ok": False, "error": "Couldn't put the radio into its bootloader"}]
        self.runner.stages = ["bootloader"]
        await self.updater.start(version="1.17.1", board="Heltec_v3")
        job = await self.finish()
        self.assertEqual(job["state"], FAILED)
        self.assertIn("bootloader", job["error"])
        # Failed before writing: no retry.
        self.assertEqual(len(self.runner.specs), 1)

    async def test_esp32_write_failure_is_retried_slowly(self):
        self.runner.results = [{"ok": False, "error": "Serial data stream stopped"}, {"ok": True}]
        await self.updater.start(version="1.17.1", board="Heltec_v3")
        job = await self.finish()
        self.assertEqual(job["state"], SUCCEEDED, job)
        self.assertEqual([spec["baud"] for spec in self.runner.specs], [460800, 115200])

    async def test_radio_reporting_the_old_version_fails(self):
        self.host.after_flash = "v1.16.0"
        await self.updater.start(version="1.17.1", board="Heltec_v3")
        job = await self.finish()
        self.assertEqual(job["state"], FAILED)
        self.assertIn("reports version v1.16.0", job["error"])

    async def test_radio_not_coming_back_is_reset_once(self):
        self.host.reconnects = False
        with mock.patch.object(updater_module, "RECONNECT_TIMEOUT_SECONDS", 0):
            await self.updater.start(version="1.17.1", board="Heltec_v3")
            job = await self.finish()
        self.assertEqual(job["state"], FAILED)
        self.assertIn("hasn't reconnected", job["error"])
        self.assertEqual([spec["action"] for spec in self.runner.specs], ["flash", "reset"])
        self.assertEqual(len(self.host.maintenance), 2)

    async def test_identity_change_is_reported(self):
        async def wait(after, timeout):
            self.transport.is_connected = True
            self.transport.device_info = {**self.transport.device_info, "ver": "v1.17.1"}
            self.transport.device_self_info = {"public_key": "cd" * 32}
            return True

        self.host.wait_for_radio = wait
        await self.updater.start(version="1.17.1", board="Heltec_v3")
        job = await self.finish()
        self.assertEqual(job["state"], SUCCEEDED)
        self.assertIn("new identity", job["warnings"][0])

    async def test_checks_before_starting(self):
        with self.assertRaisesRegex(FirmwareConflictError, "older than the installed"):
            await self.updater.start(version="1.15.0", board="Heltec_v3")
        with self.assertRaisesRegex(FirmwareConflictError, "runs Heltec_v3 or Heltec_WSL3 firmware"):
            await self.updater.start(version="1.17.1", board="RAK_4631")
        with self.assertRaisesRegex(FirmwareNotFoundError, "no companion firmware for Heltec_v3"):
            await self.updater.start(version="1.2.3", board="Heltec_v3")
        with self.assertRaisesRegex(FirmwareConflictError, "RP2040"):
            await self.updater.start(version="1.17.1", board="PicoW")
        with self.assertRaises(FirmwareError):
            await self.updater.start(version="1.17.1", board="Heltec_v3", allow_downgrade="yes")
        self.assertIsNone(self.updater.job)

        await self.updater.start(version="1.15.0", board="Heltec_v3", allow_downgrade=True)
        self.host.after_flash = "v1.15.0"
        self.assertEqual((await self.finish())["state"], SUCCEEDED)

    async def test_needs_a_usb_connection(self):
        self.settings.meshcore.connection.mode = "tcp"
        info = await self.updater.describe(board="Heltec_v3")
        self.assertFalse(info["can_update"])
        self.assertIn("need the radio on USB", info["problems"][0])
        with self.assertRaisesRegex(FirmwareConflictError, "USB"):
            await self.updater.start(version="1.17.1", board="Heltec_v3")

    async def test_a_disconnected_radio_can_be_recovered(self):
        self.transport.is_connected = False
        await self.updater.start(version="1.17.1", board="Heltec_v3")
        job = await self.finish()
        self.assertEqual(job["state"], SUCCEEDED, job)
        self.assertIsNone(job["from_version"])

    async def test_missing_port_fails_without_flashing(self):
        self.port.unlink()
        with mock.patch.object(updater_module, "PORT_WAIT_SECONDS", 0):
            await self.updater.start(version="1.17.1", board="Heltec_v3")
            job = await self.finish()
        self.assertEqual(job["state"], FAILED)
        self.assertIn("isn't there", job["error"])
        self.assertEqual(self.runner.specs, [])

    async def test_bad_download_fails_before_disconnecting(self):
        self.catalog._download = lambda url, target: target.write_bytes(b"\x00" * len(ESP_IMAGE))
        await self.updater.start(version="1.17.1", board="Heltec_v3")
        job = await self.finish()
        self.assertEqual(job["state"], FAILED)
        self.assertIn("checksum", job["error"])
        self.assertEqual(self.host.maintenance, [])

    async def test_file_that_isnt_an_update_fails_before_disconnecting(self):
        # Without a published checksum, the image check catches it.
        raw = [release("1.17.1", BOARD_FILES, data=b"\x00" * 64)]
        for item in raw[0]["assets"]:
            item.pop("digest")
        self.catalog._fetch = lambda url: json.dumps(raw).encode()
        self.catalog._download = lambda url, target: target.write_bytes(b"\x00" * 64)
        await self.updater.start(version="1.17.1", board="Heltec_v3")
        job = await self.finish()
        self.assertEqual(job["state"], FAILED)
        self.assertIn("isn't an ESP32 application image", job["error"])
        self.assertEqual(self.host.maintenance, [])


class WorkerProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_reports_unknown_jobs(self):
        events = []
        result = await run_worker({"action": "flash", "platform": "rp2040"}, events.append, 60)
        self.assertEqual(result["ok"], False)
        self.assertIn("rp2040", result["error"])

    async def test_worker_checks_the_file_before_touching_the_radio(self):
        with tempfile.NamedTemporaryFile(suffix=".bin") as handle:
            handle.write(b"not firmware")
            handle.flush()
            result = await run_worker(
                {"action": "flash", "platform": "esp32", "port": "/dev/null-radio", "file": handle.name}, [].append, 60
            )
        self.assertFalse(result["ok"])
        self.assertIn("isn't an ESP32 application image", result["error"])


if __name__ == "__main__":
    unittest.main()
