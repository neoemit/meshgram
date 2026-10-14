"""Checks on firmware files before they go near a radio, and the ESP32 partition table.

Pure Python (no flashing libraries), shared by the updater (checks a download
before it disconnects the radio) and the worker (checks again what the radio
says about itself).
"""
from __future__ import annotations

import json
import struct
import zipfile
from dataclasses import dataclass
from typing import Optional

ESP_IMAGE_MAGIC = 0xE9
# Chip IDs in an ESP-IDF application image's extended header (esptool's IMAGE_CHIP_ID).
ESP_CHIP_NAMES = {
    0: "ESP32",
    2: "ESP32-S2",
    5: "ESP32-C3",
    9: "ESP32-S3",
    12: "ESP32-C2",
    13: "ESP32-C6",
    16: "ESP32-H2",
    18: "ESP32-P4",
    23: "ESP32-C5",
    20: "ESP32-C61",
}

PARTITION_TABLE_OFFSET = 0x8000
PARTITION_TABLE_SIZE = 0xC00
_PARTITION_MAGIC = b"\xaa\x50"
_PARTITION_MD5_MAGIC = b"\xeb\xeb"
_PARTITION_ENTRY = struct.Struct("<2sBBII16sI")
PARTITION_TYPE_APP = 0x00
PARTITION_TYPE_DATA = 0x01
APP_SUBTYPE_FACTORY = 0x00
APP_SUBTYPE_OTA_0 = 0x10
DATA_SUBTYPE_OTA = 0x00

# Application-only DFU packages are all an update needs; anything else would touch the bootloader.
NRF_DFU_PARTS = ("application", "bootloader", "softdevice", "softdevice_bootloader")


class FirmwareImageError(ValueError):
    """The file isn't firmware this radio can take."""


@dataclass(frozen=True)
class EspImage:
    chip_id: int
    size: int

    @property
    def chip_name(self) -> str:
        return ESP_CHIP_NAMES.get(self.chip_id, f"chip {self.chip_id}")


@dataclass(frozen=True)
class Partition:
    type: int
    subtype: int
    offset: int
    size: int
    label: str


@dataclass(frozen=True)
class NrfPackage:
    application_size: int


def check_esp_image(data: bytes) -> EspImage:
    """An ESP32 application image (not a merged/full-flash one): magic byte, segments, chip."""
    if len(data) < 24 or data[0] != ESP_IMAGE_MAGIC:
        raise FirmwareImageError("This isn't an ESP32 application image")
    segments = data[1]
    if not 1 <= segments <= 16:
        raise FirmwareImageError("This ESP32 image has an invalid header")
    chip_id = struct.unpack_from("<H", data, 12)[0]
    return EspImage(chip_id=chip_id, size=len(data))


def parse_partition_table(data: bytes) -> list[Partition]:
    """The partitions in an ESP32 partition table (as read from flash at 0x8000)."""
    partitions = []
    for offset in range(0, len(data) - _PARTITION_ENTRY.size + 1, _PARTITION_ENTRY.size):
        magic, ptype, subtype, start, size, label, _flags = _PARTITION_ENTRY.unpack_from(data, offset)
        if magic == _PARTITION_MAGIC:
            partitions.append(Partition(ptype, subtype, start, size, label.split(b"\0", 1)[0].decode("ascii", "replace")))
        elif magic == _PARTITION_MD5_MAGIC or magic == b"\xff\xff":
            break
        else:
            raise FirmwareImageError("The radio's partition table is damaged")
    if not partitions:
        raise FirmwareImageError("The radio has no partition table")
    return partitions


def update_targets(partitions: list[Partition]) -> tuple[Partition, Optional[Partition]]:
    """Where an application update goes, and the OTA data partition to reset (if any).

    The bootloader starts the factory app, or ota_0 when there is none and the
    OTA data is blank; resetting the OTA data makes sure the new app is the one
    started even if an over-the-air update had switched to ota_1 (what the
    Arduino IDE does with boot_app0.bin).
    """
    apps = [part for part in partitions if part.type == PARTITION_TYPE_APP]
    target = next((part for part in apps if part.subtype == APP_SUBTYPE_FACTORY), None)
    if target is None:
        target = next((part for part in apps if part.subtype == APP_SUBTYPE_OTA_0), None)
    if target is None:
        raise FirmwareImageError("The radio's partition table has no application partition")
    otadata = next(
        (part for part in partitions if part.type == PARTITION_TYPE_DATA and part.subtype == DATA_SUBTYPE_OTA),
        None,
    )
    return target, otadata


def check_nrf_package(path: str) -> NrfPackage:
    """An Adafruit/Nordic DFU package (.zip) that updates the application only."""
    try:
        with zipfile.ZipFile(path) as package:
            manifest = json.loads(package.read("manifest.json")).get("manifest") or {}
            parts = {name for name in NRF_DFU_PARTS if manifest.get(name)}
            if parts != {"application"}:
                raise FirmwareImageError(
                    "This DFU package would replace "
                    + (", ".join(sorted(parts - {"application"})) or "nothing")
                    + "; only application updates are installed here"
                )
            application = manifest["application"]
            names = set(package.namelist())
            for key in ("bin_file", "dat_file"):
                if application.get(key) not in names:
                    raise FirmwareImageError("The DFU package is incomplete")
            size = package.getinfo(application["bin_file"]).file_size
            if package.testzip() is not None:
                raise FirmwareImageError("The DFU package is damaged")
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, AttributeError, TypeError) as exc:
        raise FirmwareImageError("This isn't a DFU package (.zip)") from exc
    if size <= 0:
        raise FirmwareImageError("The DFU package's application is empty")
    return NrfPackage(application_size=size)
