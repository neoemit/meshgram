"""MeshCore companion firmware releases: what's published, for which boards, and verified downloads.

Releases come from the MeshCore repository's GitHub releases (``companion-vX.Y.Z``
tags), the same files the MeshCore web flasher installs. Each asset is named
``<board>_companion_radio_<usb|ble>-v<version>-<commit><suffix>``; a board's
files tell its platform (``.zip`` DFU package: nRF52, app ``.bin`` only: ESP32,
``.uf2`` with ``.bin``: RP2040, ``.hex``: STM32).

The radio reports its board as a "manufacturer name" (DEVICE_INFO ``model``,
"Heltec V3"); ``MODEL_BOARDS`` maps those to release board names (from the
firmware's variants), and boards named alike are matched when it doesn't list one.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

LOGGER = logging.getLogger(__name__)

RELEASES_URL = "https://api.github.com/repos/meshcore-dev/MeshCore/releases"
RELEASE_TAG_PREFIX = "companion-v"
# Releases come out three at a time (companion, repeater, room server) with ~300 files each
# (~350 KB of JSON a release): the newest 30 hold the last ten or so companion versions.
RELEASE_PAGES = 1
RELEASES_PER_PAGE = 30
MAX_RELEASE_LIST_BYTES = 32 * 1024 * 1024
CATALOG_TTL_SECONDS = 60 * 60
# A refresh asked for from the panel; GitHub allows 60 unauthenticated requests an hour.
MIN_REFRESH_SECONDS = 60
HTTP_TIMEOUT_SECONDS = 30
MAX_FIRMWARE_BYTES = 16 * 1024 * 1024
DOWNLOAD_HOSTS = {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
USER_AGENT = "Meshgram firmware updater"

# The firmware Meshgram talks to: the companion radio over USB serial. The BLE build would
# lock Meshgram out (it listens on Bluetooth instead of the USB port).
ROLE_USB = "companion_radio_usb"

PLATFORM_ESP32 = "esp32"
PLATFORM_NRF52 = "nrf52"
PLATFORM_RP2040 = "rp2040"
PLATFORM_STM32 = "stm32"
PLATFORM_TITLES = {
    PLATFORM_ESP32: "ESP32",
    PLATFORM_NRF52: "nRF52",
    PLATFORM_RP2040: "RP2040",
    PLATFORM_STM32: "STM32",
}
SUPPORTED_PLATFORMS = {PLATFORM_ESP32, PLATFORM_NRF52}
UNSUPPORTED_PLATFORM_HINTS = {
    PLATFORM_RP2040: "RP2040 boards are updated by copying the .uf2 file onto the drive they show in BOOTSEL mode",
    PLATFORM_STM32: "STM32 boards are updated with an STM32 programmer",
}

_ASSET_NAME = re.compile(
    r"^(?P<board>.+?)_(?P<role>companion_radio_[a-z0-9]+)_?-v(?P<version>\d+(?:\.\d+)*)(?:-(?P<commit>[0-9a-f]{6,40}))?"
    r"(?P<suffix>-merged\.bin|\.bin|\.zip|\.uf2|\.hex)$"
)
_VERSION = re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?")

# DEVICE_INFO model -> release board names (MeshCore's variants/*: getManufacturerName and
# their companion_radio_usb environments). A model with several boards is asked about.
MODEL_BOARDS: dict[str, tuple[str, ...]] = {
    "Elecrow ThinkNode-M1": ("ThinkNode_M1",),
    "Elecrow ThinkNode M2": ("ThinkNode_M2",),
    "Elecrow ThinkNode M3": ("ThinkNode_M3",),
    "Elecrow ThinkNode M5": ("ThinkNode_M5",),
    "Elecrow ThinkNode M6": ("ThinkNode_M6",),
    "Elecrow ThinkNode M7": ("ThinkNode_M7",),
    "Elecrow ThinkNode M9": ("ThinkNode_M9",),
    "GAT562 30S Mesh Kit": ("GAT562_30S_Mesh_Kit",),
    "GAT562 EVB Pro": ("GAT562_Mesh_EVB_Pro",),
    "GAT562 Mesh Tracker Pro": ("GAT562_Mesh_Tracker_Pro",),
    "Heltec CT62": ("Heltec_ct62",),
    "Heltec E213": ("Heltec_E213",),
    "Heltec E290": ("Heltec_E290",),
    "Heltec Mesh Solar": ("Heltec_mesh_solar",),
    "Heltec MeshPocket": ("Mesh_pocket",),
    "Heltec RC32": ("heltec_rc32", "heltec_rc32_without_display"),
    "Heltec T096": ("Heltec_t096",),
    "Heltec T1": ("Heltec_t1",),
    "Heltec T114": ("Heltec_t114", "Heltec_t114_without_display"),
    "Heltec T190": ("Heltec_T190",),
    "Heltec Tower V2": ("Heltec_tower_v2",),
    "Heltec Tracker V2": ("heltec_tracker_v2",),
    "Heltec V2": ("Heltec_v2",),
    "Heltec V3": ("Heltec_v3", "Heltec_WSL3"),
    "Heltec V4 OLED": ("heltec_v4",),
    "Heltec V4.3 OLED": ("heltec_v4",),
    "Heltec V4 TFT": ("heltec_v4_tft",),
    "Heltec V4.3 TFT": ("heltec_v4_tft",),
    "Heltec V4 R8 OLED": ("heltec_v4_r8",),
    "Heltec V4 R8 TFT": ("heltec_v4_r8_tft",),
    "Heltec Wireless Paper": ("Heltec_Wireless_Paper",),
    "Ikoka Handheld E22 30dBm (Xiao_nrf52)": (
        "ikoka_handheld_nrf_e22_30dbm_096",
        "ikoka_handheld_nrf_e22_30dbm_096_rotated",
    ),
    "Keepteen LT1": ("KeepteenLT1",),
    "LILYGO T-LoRa V2.1-1.6": ("LilyGo_TLora_V2_1_1_6",),
    "LilyGO T-ETH Elite": ("LilyGo_TETH_Elite_sx1262",),
    "LilyGo T-Beam": ("Tbeam_SX1262", "Tbeam_SX1276"),
    "LilyGo T-Beam 1W": ("LilyGo_TBeam_1W",),
    "LilyGo T-Deck": ("LilyGo_TDeck",),
    "LilyGo T-Echo": ("LilyGo_T-Echo",),
    "LilyGo T-Echo Card": ("LilyGo_T-Echo_Card",),
    "LilyGo T-Echo Lite": ("LilyGo_T-Echo-Lite_non_shell", "LilyGo_T-Echo-Lite"),
    "Meshadventurer": ("Meshadventurer_sx1262", "Meshadventurer_sx1268"),
    "Meshnology W12": ("meshnology_w12",),
    "Meshtiny": ("Meshtiny",),
    "Minewsemi": ("Minewsemi_me25ls01",),
    "muzi works R1 Neo": ("R1Neo",),
    "Nano G2 Ultra": ("Nano_G2_Ultra",),
    "Pico W": ("PicoW",),
    "ProMicro DIY": ("ProMicro",),
    "RAK 11310": ("RAK_11310",),
    "RAK 3112": ("RAK_3112",),
    "RAK 3401": ("RAK_3401",),
    "RAK 3x72": ("RAK_3x72",),
    "RAK 4631": ("RAK_4631",),
    "RAK WisMesh Tag": ("RAK_WisMesh_Tag",),
    "Seeed SenseCAP MeshTracker X1": ("MeshTracker_X1",),
    "Seeed SenseCap Solar": ("SenseCap_Solar",),
    "Seeed Tracker T1000-E": ("t1000e",),
    "Seeed Wio E5 Dev Board": ("wio-e5",),
    "Seeed Wio E5 mini": ("wio-e5-mini",),
    "Seeed Wio Tracker L1": ("WioTrackerL1",),
    "Seeed Wio WM1110": ("wio_wm1110",),
    "Seeed Xiao-nrf52": ("Xiao_nrf52",),
    "Station G2": ("Station_G2",),
    "Station G3 ESP32": ("Station_G3_ESP32",),
    "Tiny Relay": ("Tiny_Relay",),
    "Unit C6L": ("M5Stack_Unit_C6L",),
    "Waveshare RP2040-LoRa": ("waveshare_rp2040_lora",),
    "Xiao C3": ("Xiao_C3",),
    "Xiao C6": ("Xiao_C6",),
    "Xiao RP2040": ("Xiao_rp2040",),
    "Xiao S3": ("Xiao_S3",),
    "Xiao S3 WIO": ("Xiao_S3_WIO",),
}


class FirmwareCatalogError(RuntimeError):
    """The releases (or a file) couldn't be fetched from GitHub."""


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


_MODEL_BOARDS_BY_KEY = {_key(model): boards for model, boards in MODEL_BOARDS.items()}


def parse_version(text: Any) -> Optional[tuple[int, int, int]]:
    """``(1, 17, 1)`` from "v1.17.1", "1.17.1-abc123" or "companion-v1.17.1"; None if there's no number."""
    match = _VERSION.search(str(text or ""))
    if match is None:
        return None
    return tuple(int(part or 0) for part in match.groups())  # type: ignore[return-value]


def format_version(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


@dataclass(frozen=True)
class FirmwareAsset:
    name: str
    board: str
    role: str
    version: str
    commit: Optional[str]
    suffix: str
    size: int
    url: str
    sha256: Optional[str]

    @property
    def merged(self) -> bool:
        return self.suffix == "-merged.bin"


@dataclass(frozen=True)
class FirmwareRelease:
    version: str
    tag: str
    name: str
    published_at: Optional[str]
    prerelease: bool
    notes_url: Optional[str]
    assets: tuple[FirmwareAsset, ...]

    @property
    def version_tuple(self) -> tuple[int, int, int]:
        return parse_version(self.version) or (0, 0, 0)

    def board_assets(self, board: str, role: str = ROLE_USB) -> list[FirmwareAsset]:
        return [asset for asset in self.assets if asset.board == board and asset.role == role]


def parse_asset(raw: dict[str, Any]) -> Optional[FirmwareAsset]:
    name = str(raw.get("name") or "")
    match = _ASSET_NAME.match(name)
    url = str(raw.get("browser_download_url") or "")
    if match is None or not url:
        return None
    digest = str(raw.get("digest") or "")
    sha256 = digest[len("sha256:"):].lower() if digest.startswith("sha256:") else None
    try:
        size = int(raw.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return FirmwareAsset(
        name=name,
        board=match["board"],
        role=match["role"],
        version=match["version"],
        commit=match["commit"],
        suffix=match["suffix"],
        size=size,
        url=url,
        sha256=sha256,
    )


def parse_releases(data: Any) -> list[FirmwareRelease]:
    """Companion firmware releases from the GitHub API's release list, newest version first."""
    releases = []
    for raw in data if isinstance(data, list) else []:
        if not isinstance(raw, dict) or raw.get("draft"):
            continue
        tag = str(raw.get("tag_name") or "")
        if not tag.startswith(RELEASE_TAG_PREFIX) or parse_version(tag) is None:
            continue
        assets = tuple(asset for asset in map(parse_asset, raw.get("assets") or []) if asset is not None)
        releases.append(
            FirmwareRelease(
                version=format_version(parse_version(tag)),  # type: ignore[arg-type]
                tag=tag,
                name=str(raw.get("name") or tag),
                published_at=raw.get("published_at"),
                prerelease=bool(raw.get("prerelease")),
                notes_url=raw.get("html_url"),
                assets=assets,
            )
        )
    releases.sort(key=lambda release: release.version_tuple, reverse=True)
    return releases


def board_platform(files: list[FirmwareAsset]) -> Optional[str]:
    suffixes = {asset.suffix for asset in files}
    if ".zip" in suffixes:
        return PLATFORM_NRF52
    if ".hex" in suffixes:
        return PLATFORM_STM32
    if ".uf2" in suffixes:
        return PLATFORM_RP2040
    if suffixes & {".bin", "-merged.bin"}:
        return PLATFORM_ESP32
    return None


def update_file(files: list[FirmwareAsset], platform: Optional[str]) -> Optional[FirmwareAsset]:
    """The file that updates a board in place: the nRF52 DFU package, the ESP32 app image (not merged)."""
    wanted = {PLATFORM_NRF52: ".zip", PLATFORM_ESP32: ".bin"}.get(platform or "")
    return next((asset for asset in files if asset.suffix == wanted), None)


def boards_for_model(model: Any, boards: set[str]) -> list[str]:
    """The release boards a radio reporting this model may be, best guesses first."""
    key = _key(str(model or ""))
    if not key:
        return []
    known = [board for board in _MODEL_BOARDS_BY_KEY.get(key, ()) if board in boards]
    if known:
        return known
    # A board the table doesn't know yet, named like its model ("Heltec E213" -> Heltec_E213).
    return sorted(board for board in boards if _key(board) == key)


@dataclass
class CatalogState:
    releases: list[FirmwareRelease] = field(default_factory=list)
    fetched_at: Optional[float] = None
    error: Optional[str] = None
    attempted_at: Optional[float] = None


Fetch = Callable[[str], bytes]


def _http_get(url: str, *, accept: str = "application/vnd.github+json", limit: int = MAX_RELEASE_LIST_BYTES) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:  # noqa: S310 - https only
            data = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 403 and exc.headers.get("X-RateLimit-Remaining") == "0":
            raise FirmwareCatalogError("GitHub's rate limit is reached; try again in an hour") from None
        raise FirmwareCatalogError(f"GitHub answered {exc.code} {exc.reason}") from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise FirmwareCatalogError(f"Can't reach GitHub: {reason}") from None
    if len(data) > limit:
        raise FirmwareCatalogError("GitHub's answer is unexpectedly large")
    return data


class FirmwareCatalog:
    """The published releases (cached), and verified, cached downloads of their files."""

    def __init__(self, cache_dir: Path, *, fetch: Optional[Fetch] = None, download: Optional[Callable[[str, Path], None]] = None):
        self.cache_dir = cache_dir
        self._fetch = fetch or _http_get
        self._download = download or _download_to
        self.state = CatalogState()
        self._lock = asyncio.Lock()

    async def releases(self, *, refresh: bool = False) -> CatalogState:
        """The releases, fetched again when old (or asked to, at most every ``MIN_REFRESH_SECONDS``).

        A failed fetch keeps the releases fetched before and reports the error.
        """
        async with self._lock:
            now = time.monotonic()
            state = self.state
            due = refresh or state.fetched_at is None or now - state.fetched_at > CATALOG_TTL_SECONDS
            throttled = state.attempted_at is not None and now - state.attempted_at < MIN_REFRESH_SECONDS
            if due and not throttled:
                state.attempted_at = now
                try:
                    state.releases = await asyncio.to_thread(self._fetch_all)
                    state.fetched_at = time.monotonic()
                    state.error = None
                except FirmwareCatalogError as exc:
                    state.error = str(exc)
                    LOGGER.warning("Firmware releases couldn't be fetched: %s", exc)
            return state

    def _fetch_all(self) -> list[FirmwareRelease]:
        raw: list[Any] = []
        for page in range(1, RELEASE_PAGES + 1):
            url = f"{RELEASES_URL}?per_page={RELEASES_PER_PAGE}&page={page}"
            try:
                batch = json.loads(self._fetch(url))
            except (ValueError, UnicodeDecodeError) as exc:
                raise FirmwareCatalogError("GitHub's release list isn't valid JSON") from exc
            if not isinstance(batch, list):
                raise FirmwareCatalogError("GitHub's release list isn't a list")
            raw.extend(batch)
            if len(batch) < RELEASES_PER_PAGE:
                break
        return parse_releases(raw)

    async def download(self, release: FirmwareRelease, asset: FirmwareAsset) -> Path:
        """The asset's file, downloaded once and checked against GitHub's size and SHA-256."""
        host = urllib.parse.urlsplit(asset.url).hostname or ""
        if urllib.parse.urlsplit(asset.url).scheme != "https" or host not in DOWNLOAD_HOSTS:
            raise FirmwareCatalogError(f"Refusing to download from {host or asset.url}")
        if not 0 < asset.size <= MAX_FIRMWARE_BYTES:
            raise FirmwareCatalogError("The firmware file's size is implausible")
        folder = self.cache_dir / _safe_name(release.tag)
        path = folder / _safe_name(asset.name)
        if path.is_file() and _matches(path, asset):
            return path
        await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(dir=folder, prefix=".download-")
        os.close(handle)
        temp = Path(temp_name)
        try:
            await asyncio.to_thread(self._download, asset.url, temp)
            if not _matches(temp, asset):
                raise FirmwareCatalogError(
                    "The downloaded file doesn't match the release's checksum; it was discarded"
                )
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
        return path

    def prune(self, keep: Path) -> None:
        """Drop downloads other than ``keep`` (one firmware's worth stays cached)."""
        if not self.cache_dir.is_dir():
            return
        for folder in self.cache_dir.iterdir():
            if not folder.is_dir():
                continue
            for file in folder.iterdir():
                if file != keep:
                    file.unlink(missing_ok=True)
            if folder != keep.parent and not any(folder.iterdir()):
                folder.rmdir()


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name).lstrip(".")
    return cleaned or "file"


def _matches(path: Path, asset: FirmwareAsset) -> bool:
    if path.stat().st_size != asset.size:
        return False
    if asset.sha256 is None:
        return True
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest() == asset.sha256


def _download_to(url: str, target: Path) -> None:
    request = urllib.request.Request(url, headers={"Accept": "application/octet-stream", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response, target.open("wb") as out:  # noqa: S310
            final_host = urllib.parse.urlsplit(response.geturl()).hostname or ""
            if final_host not in DOWNLOAD_HOSTS:
                raise FirmwareCatalogError(f"The download was redirected to {final_host}")
            written = 0
            for chunk in iter(lambda: response.read(1 << 16), b""):
                written += len(chunk)
                if written > MAX_FIRMWARE_BYTES:
                    raise FirmwareCatalogError("The firmware file is unexpectedly large")
                out.write(chunk)
    except urllib.error.HTTPError as exc:
        raise FirmwareCatalogError(f"The download failed: GitHub answered {exc.code} {exc.reason}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise FirmwareCatalogError(f"The download failed: {getattr(exc, 'reason', exc)}") from None
