import asyncio
import hashlib
import io
import json
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from meshgram.firmware import catalog as catalog_module
from meshgram.firmware.catalog import (
    PLATFORM_ESP32,
    PLATFORM_NRF52,
    PLATFORM_RP2040,
    FirmwareCatalog,
    FirmwareCatalogError,
    board_platform,
    boards_for_model,
    parse_releases,
    parse_version,
    update_file,
)
from meshgram.firmware.images import (
    FirmwareImageError,
    check_esp_image,
    check_nrf_package,
    parse_partition_table,
    update_targets,
)

BASE = "https://github.com/meshcore-dev/MeshCore/releases/download"


def asset(name, tag, data=b"firmware", digest=True):
    raw = {"name": name, "size": len(data), "browser_download_url": f"{BASE}/{tag}/{name}"}
    if digest:
        raw["digest"] = "sha256:" + hashlib.sha256(data).hexdigest()
    return raw


def release(version, names, prerelease=False, data=b"firmware"):
    tag = f"companion-v{version}"
    return {
        "tag_name": tag,
        "name": f"Companion Firmware v{version}",
        "published_at": "2026-08-14T13:32:31Z",
        "prerelease": prerelease,
        "html_url": f"https://github.com/meshcore-dev/MeshCore/releases/tag/{tag}",
        "assets": [asset(name.format(v=version), tag, data) for name in names],
    }


BOARD_FILES = [
    "Heltec_v3_companion_radio_usb-v{v}-d929643.bin",
    "Heltec_v3_companion_radio_ble-v{v}-d929643.bin",
    "Heltec_WSL3_companion_radio_usb-v{v}-d929643.bin",
    "RAK_4631_companion_radio_usb-v{v}-d929643.zip",
    "RAK_4631_companion_radio_usb-v{v}-d929643.uf2",
    "PicoW_companion_radio_usb-v{v}-d929643.uf2",
    "PicoW_companion_radio_usb-v{v}-d929643.bin",
    "Heltec_E213_companion_radio_usb-v{v}-d929643.bin",
    "Heltec_E213_companion_radio_usb-v{v}-d929643-merged.bin",
]


class ReleaseParsingTests(unittest.TestCase):
    def test_versions(self):
        self.assertEqual(parse_version("v1.17.1"), (1, 17, 1))
        self.assertEqual(parse_version("companion-v1.9"), (1, 9, 0))
        self.assertEqual(parse_version("1.10.0-abc123"), (1, 10, 0))
        self.assertIsNone(parse_version(""))
        self.assertIsNone(parse_version(None))

    def test_parses_companion_releases_newest_first(self):
        raw = [
            release("1.9.0", BOARD_FILES),
            {"tag_name": "repeater-v1.17.1", "assets": []},
            {"tag_name": "companion-v1.18.0", "draft": True, "assets": []},
            release("1.17.1", BOARD_FILES),
            release("1.10.0", BOARD_FILES, prerelease=True),
        ]
        releases = parse_releases(raw)
        self.assertEqual([r.version for r in releases], ["1.17.1", "1.10.0", "1.9.0"])
        self.assertTrue(releases[1].prerelease)
        files = releases[0].board_assets("Heltec_v3")
        self.assertEqual([f.name for f in files], ["Heltec_v3_companion_radio_usb-v1.17.1-d929643.bin"])
        self.assertEqual(files[0].commit, "d929643")
        self.assertEqual(len(files[0].sha256), 64)

    def test_platforms_and_update_files(self):
        newest = parse_releases([release("1.17.1", BOARD_FILES)])[0]
        rak = newest.board_assets("RAK_4631")
        self.assertEqual(board_platform(rak), PLATFORM_NRF52)
        self.assertTrue(update_file(rak, PLATFORM_NRF52).name.endswith(".zip"))
        e213 = newest.board_assets("Heltec_E213")
        self.assertEqual(board_platform(e213), PLATFORM_ESP32)
        # The app image, not the merged full-flash one.
        self.assertTrue(update_file(e213, PLATFORM_ESP32).name.endswith("d929643.bin"))
        self.assertEqual(board_platform(newest.board_assets("PicoW")), PLATFORM_RP2040)
        self.assertIsNone(update_file(newest.board_assets("PicoW"), PLATFORM_RP2040))

    def test_boards_for_model(self):
        boards = {"Heltec_v3", "Heltec_WSL3", "RAK_4631", "Heltec_E213", "t1000e"}
        self.assertEqual(boards_for_model("Heltec V3", boards), ["Heltec_v3", "Heltec_WSL3"])
        self.assertEqual(boards_for_model("RAK 4631", boards), ["RAK_4631"])
        self.assertEqual(boards_for_model("Seeed Tracker T1000-E", boards), ["t1000e"])
        # Not in the table: matched by name.
        self.assertEqual(boards_for_model("Heltec E213", {"Heltec_E213"}), ["Heltec_E213"])
        self.assertEqual(boards_for_model("Generic ESP32", boards), [])
        self.assertEqual(boards_for_model(None, boards), [])


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.cache = Path(tempdir.name)
        self.fetches = []
        self.answer = json.dumps([release("1.17.1", BOARD_FILES)]).encode()

    def fetch(self, url):
        self.fetches.append(url)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    async def test_releases_are_cached_and_refreshes_throttled(self):
        catalog = FirmwareCatalog(self.cache, fetch=self.fetch)
        state = await catalog.releases()
        self.assertEqual([r.version for r in state.releases], ["1.17.1"])
        await catalog.releases()
        await catalog.releases(refresh=True)  # too soon after the last fetch
        self.assertEqual(len(self.fetches), 1)
        with mock.patch.object(catalog_module, "MIN_REFRESH_SECONDS", 0):
            await catalog.releases(refresh=True)
        self.assertEqual(len(self.fetches), 2)

    async def test_a_failed_fetch_keeps_what_was_fetched_before(self):
        catalog = FirmwareCatalog(self.cache, fetch=self.fetch)
        await catalog.releases()
        self.answer = FirmwareCatalogError("Can't reach GitHub")
        with mock.patch.object(catalog_module, "MIN_REFRESH_SECONDS", 0):
            state = await catalog.releases(refresh=True)
        self.assertEqual(state.error, "Can't reach GitHub")
        self.assertEqual(len(state.releases), 1)

    async def test_download_checks_the_checksum(self):
        data = b"\xe9" * 100
        rel = parse_releases([release("1.17.1", BOARD_FILES, data=data)])[0]
        file = rel.board_assets("Heltec_v3")[0]
        downloads = []

        def download(url, target):
            downloads.append(url)
            target.write_bytes(data)

        catalog = FirmwareCatalog(self.cache, fetch=self.fetch, download=download)
        path = await catalog.download(rel, file)
        self.assertEqual(path.read_bytes(), data)
        await catalog.download(rel, file)  # cached
        self.assertEqual(len(downloads), 1)

        catalog = FirmwareCatalog(self.cache / "other", fetch=self.fetch, download=lambda url, target: target.write_bytes(b"x" * 100))
        with self.assertRaisesRegex(FirmwareCatalogError, "checksum"):
            await catalog.download(rel, file)
        self.assertEqual(list((self.cache / "other").rglob("*.bin")), [])

    async def test_download_only_from_github(self):
        rel = parse_releases([release("1.17.1", BOARD_FILES)])[0]
        file = rel.board_assets("Heltec_v3")[0]
        evil = type(file)(**{**file.__dict__, "url": "https://example.com/firmware.bin"})
        with self.assertRaisesRegex(FirmwareCatalogError, "Refusing"):
            await FirmwareCatalog(self.cache, fetch=self.fetch).download(rel, evil)

    async def test_prune_keeps_one_download(self):
        catalog = FirmwareCatalog(self.cache, fetch=self.fetch)
        old = self.cache / "companion-v1.9.0" / "old.bin"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"old")
        keep = self.cache / "companion-v1.17.1" / "new.bin"
        keep.parent.mkdir(parents=True)
        keep.write_bytes(b"new")
        await asyncio.to_thread(catalog.prune, keep)
        self.assertFalse(old.parent.exists())
        self.assertTrue(keep.exists())


def esp_image(chip_id=9, segments=6):
    header = bytes([0xE9, segments, 2, 0x2F]) + struct.pack("<I", 0x40377630)
    extended = bytes([0xEE, 0, 0, 0]) + struct.pack("<H", chip_id) + bytes(10)
    return header + extended + bytes(64)


def partition(ptype, subtype, offset, size, label):
    return struct.pack("<2sBBII16sI", b"\xaa\x50", ptype, subtype, offset, size, label.encode().ljust(16, b"\0"), 0)


ARDUINO_TABLE = (
    partition(1, 2, 0x9000, 0x5000, "nvs")
    + partition(1, 0, 0xE000, 0x2000, "otadata")
    + partition(0, 0x10, 0x10000, 0x140000, "app0")
    + partition(0, 0x11, 0x150000, 0x140000, "app1")
    + partition(1, 0x82, 0x290000, 0x160000, "spiffs")
    + b"\xeb\xeb" + b"\xff" * 30
    + b"\xff" * 64
)


class ImageTests(unittest.TestCase):
    def test_esp_image(self):
        image = check_esp_image(esp_image())
        self.assertEqual((image.chip_id, image.chip_name), (9, "ESP32-S3"))
        with self.assertRaises(FirmwareImageError):
            check_esp_image(b"PK\x03\x04" + bytes(40))
        with self.assertRaises(FirmwareImageError):
            check_esp_image(esp_image(segments=0))

    def test_partition_table_targets_app0_and_resets_otadata(self):
        app, otadata = update_targets(parse_partition_table(ARDUINO_TABLE))
        self.assertEqual((app.label, app.offset, app.size), ("app0", 0x10000, 0x140000))
        self.assertEqual((otadata.offset, otadata.size), (0xE000, 0x2000))

    def test_factory_app_wins(self):
        table = partition(0, 0x10, 0x110000, 0x100000, "ota_0") + partition(0, 0, 0x10000, 0x100000, "factory") + b"\xff" * 32
        app, otadata = update_targets(parse_partition_table(table))
        self.assertEqual(app.label, "factory")
        self.assertIsNone(otadata)

    def test_bad_partition_tables(self):
        with self.assertRaisesRegex(FirmwareImageError, "no partition table"):
            parse_partition_table(b"\xff" * 0xC00)
        with self.assertRaisesRegex(FirmwareImageError, "damaged"):
            parse_partition_table(b"\x00" * 0xC00)
        with self.assertRaisesRegex(FirmwareImageError, "no application"):
            update_targets(parse_partition_table(partition(1, 2, 0x9000, 0x5000, "nvs")))

    def _package(self, manifest, files=("firmware.bin", "firmware.dat")):
        handle = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        self.addCleanup(Path(handle.name).unlink)
        with zipfile.ZipFile(handle, "w") as package:
            package.writestr("manifest.json", json.dumps({"manifest": manifest}))
            for name in files:
                package.writestr(name, b"\x01" * 1000)
        handle.close()
        return handle.name

    def test_nrf_package(self):
        app = {"application": {"bin_file": "firmware.bin", "dat_file": "firmware.dat"}, "dfu_version": 0.5}
        self.assertEqual(check_nrf_package(self._package(app)).application_size, 1000)
        with self.assertRaisesRegex(FirmwareImageError, "bootloader"):
            check_nrf_package(self._package({**app, "bootloader": {"bin_file": "firmware.bin", "dat_file": "firmware.dat"}}))
        with self.assertRaisesRegex(FirmwareImageError, "incomplete"):
            check_nrf_package(self._package(app, files=("firmware.bin",)))
        with tempfile.NamedTemporaryFile(suffix=".zip") as handle:
            handle.write(io.BytesIO(b"not a zip").read())
            handle.flush()
            with self.assertRaisesRegex(FirmwareImageError, "isn't a DFU package"):
                check_nrf_package(handle.name)


if __name__ == "__main__":
    unittest.main()
