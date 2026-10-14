"""Installs firmware on a radio over USB serial. Run by the updater as its own process:

    python -m meshgram.firmware.worker < job.json

The job is one JSON object on stdin:

    {"action": "flash", "platform": "esp32"|"nrf52", "port": "/dev/ttyUSB0", "file": "...", "baud": 460800}
    {"action": "reset", "platform": "esp32", "port": "/dev/ttyUSB0"}

Progress goes to stdout as JSON lines: ``{"event": "stage", "stage": ..., "text": ...}``,
``{"event": "progress", "percent": ...}``, ``{"event": "log", "text": ...}``, and last
``{"event": "result", "ok": true|false, "error"?: ...}``. Anything the libraries print
goes to stderr.

Updates never erase: ESP32 radios get the application image written over their
app partition (found in the radio's own partition table) and the OTA boot
selection reset; nRF52 radios get an application-only DFU package through the
Adafruit bootloader, which leaves the bootloader, SoftDevice and the settings
filesystem alone. Both put the radio into its bootloader themselves (esptool's
reset sequences; the 1200 baud "touch" for nRF52) and start the new firmware
when done.
"""
from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

from .images import (
    PARTITION_TABLE_OFFSET,
    PARTITION_TABLE_SIZE,
    FirmwareImageError,
    check_esp_image,
    check_nrf_package,
    parse_partition_table,
    update_targets,
)

ESP_CONNECT_BAUD = 115200
ESP_DEFAULT_FLASH_BAUD = 460800
ESP_CONNECT_ATTEMPTS = 3
NRF_DFU_BAUD = 115200
NRF_DFU_PACKET_SIZE = 512
TOUCH_BAUD = 1200
# How long a radio takes to drop off USB and come back as its bootloader (or the other way).
REENUMERATE_SECONDS = 20.0
PORT_POLL_SECONDS = 0.25

Emit = Callable[..., None]


class FlashError(RuntimeError):
    """Flashing failed; the message says what to do."""


# --- Serial port helpers --------------------------------------------------------------------


def usb_identity(port: str) -> dict[str, Any]:
    """The USB location/serial number of a port, to find the device again after it re-enumerates."""
    try:
        from serial.tools import list_ports
    except ImportError:  # pragma: no cover - pyserial comes with both tools
        return {}
    real = os.path.realpath(port)
    for info in list_ports.comports():
        if os.path.realpath(info.device) == real:
            return {"location": info.location, "serial_number": info.serial_number, "vid": info.vid}
    return {}


def find_port(port: str, identity: dict[str, Any], *, timeout: float = REENUMERATE_SECONDS) -> str:
    """The port the device is on now: the same USB socket (location), the same serial number, or ``port``."""
    from serial.tools import list_ports

    deadline = time.monotonic() + timeout
    while True:
        for info in list_ports.comports():
            if identity.get("location") and info.location == identity["location"]:
                return info.device
            if identity.get("serial_number") and info.serial_number == identity["serial_number"]:
                return info.device
        if os.path.exists(port):
            return port
        if time.monotonic() >= deadline:
            raise FlashError(f"The radio didn't come back on USB ({port})")
        time.sleep(PORT_POLL_SECONDS)


def touch(port: str) -> None:
    """Open and close the port at 1200 baud: boards with native USB take it as "go to the bootloader"."""
    import serial

    try:
        with serial.Serial(port, TOUCH_BAUD) as link:
            link.dtr = False
            time.sleep(0.1)
    except (OSError, serial.SerialException) as exc:
        raise FlashError(f"Can't open {port}: {exc}") from None


# --- ESP32 ------------------------------------------------------------------------------------


def _esptool_logger(emit: Emit):
    from esptool.logger import TemplateLogger

    class Logger(TemplateLogger):
        def __init__(self) -> None:
            self.last_percent = -1

        def print(self, *args: Any, **kwargs: Any) -> None:
            text = " ".join(str(arg) for arg in args).strip()
            if text:
                emit("log", text=text)

        def note(self, message: str) -> None:
            emit("log", text=f"Note: {message}")

        def warning(self, message: str) -> None:
            emit("log", text=f"Warning: {message}")

        def error(self, message: str) -> None:
            emit("log", text=f"Error: {message}")

        def stage(self, finish: bool = False) -> None:
            pass

        def progress_bar(self, cur_iter: int, total_iters: int, prefix: str = "", suffix: str = "", bar_length: int = 30) -> None:
            if total_iters <= 0:
                return
            percent = int(100 * cur_iter / total_iters)
            if percent != self.last_percent:
                self.last_percent = percent
                emit("progress", percent=percent)

        def set_verbosity(self, verbosity: str) -> None:
            pass

    return Logger()


def _connect_esp(port: str, emit: Emit) -> Any:
    """Reset the radio into its ROM bootloader (or catch it there) and connect."""
    from esptool.cmds import detect_chip

    errors = []
    for mode in ("default-reset", "usb-reset"):
        try:
            return detect_chip(port, ESP_CONNECT_BAUD, mode, connect_attempts=ESP_CONNECT_ATTEMPTS)
        except Exception as exc:  # esptool raises FatalError and serial errors
            errors.append(f"{mode}: {exc}")
            emit("log", text=f"Couldn't enter the bootloader with {mode}: {exc}")
    # Native USB boards running TinyUSB CDC reset into the bootloader on a 1200 baud touch.
    identity = usb_identity(port)
    try:
        touch(port)
        time.sleep(1.5)
        port = find_port(port, identity)
        return detect_chip(port, ESP_CONNECT_BAUD, "no-reset", connect_attempts=ESP_CONNECT_ATTEMPTS)
    except Exception as exc:
        errors.append(f"1200 baud touch: {exc}")
    raise FlashError(
        "Couldn't put the radio into its bootloader. Hold its BOOT button, press and release RESET, "
        "let go of BOOT, then try again. (" + "; ".join(errors) + ")"
    )


def flash_esp32(job: dict[str, Any], emit: Emit) -> None:
    from esptool.cmds import attach_flash, read_flash, reset_chip, run_stub, write_flash
    from esptool.logger import log

    log.set_logger(_esptool_logger(emit))
    image_data = Path(job["file"]).read_bytes()
    image = check_esp_image(image_data)

    emit("stage", stage="bootloader", text="Putting the radio into its bootloader")
    esp = _connect_esp(job["port"], emit)
    with esp:
        chip = esp.CHIP_NAME
        emit("log", text=f"Connected to the {chip} bootloader")
        if getattr(esp, "IMAGE_CHIP_ID", None) != image.chip_id:
            reset_chip(esp, "hard-reset")
            raise FlashError(f"This firmware is for {image.chip_name}, but the radio is an {chip}; nothing was written")
        esp = run_stub(esp)
        baud = int(job.get("baud") or ESP_DEFAULT_FLASH_BAUD)
        if baud != ESP_CONNECT_BAUD:
            try:
                esp.change_baud(baud)
            except Exception as exc:
                emit("log", text=f"Staying at {ESP_CONNECT_BAUD} baud ({exc})")
        attach_flash(esp)

        table = read_flash(esp, PARTITION_TABLE_OFFSET, PARTITION_TABLE_SIZE, no_progress=True)
        app, otadata = update_targets(parse_partition_table(table or b""))
        if image.size > app.size:
            raise FlashError(
                f"The firmware ({image.size} bytes) doesn't fit the radio's app partition ({app.size} bytes); "
                "install it with the MeshCore web flasher (erase and install)"
            )
        writes: list[tuple[int, bytes]] = [(app.offset, image_data)]
        if otadata is not None:
            writes.append((otadata.offset, b"\xff" * otadata.size))
        emit("stage", stage="writing", text=f"Writing the firmware ({app.label} at {app.offset:#x})")
        emit("progress", percent=0)
        write_flash(esp, writes, compress=True)
        emit("stage", stage="restarting", text="Restarting the radio")
        reset_chip(esp, "hard-reset")


def reset_esp32(job: dict[str, Any], emit: Emit) -> None:
    from esptool.cmds import reset_chip
    from esptool.logger import log

    log.set_logger(_esptool_logger(emit))
    with _connect_esp(job["port"], emit) as esp:
        reset_chip(esp, "hard-reset")


# --- nRF52 ------------------------------------------------------------------------------------


def flash_nrf52(job: dict[str, Any], emit: Emit) -> None:
    from nordicsemi.dfu.dfu import Dfu
    from nordicsemi.dfu.dfu_transport import DfuEvent
    from nordicsemi.dfu.dfu_transport_serial import DfuTransportSerial

    package = check_nrf_package(job["file"])
    frames = max(1, math.ceil(package.application_size / NRF_DFU_PACKET_SIZE))
    last = {"percent": -1}

    def on_progress(progress: int = 0, done: bool = False, log_message: str = "") -> None:
        # ``progress`` counts the packets sent (then a closing 100); the bar only moves forward.
        percent = min(99, int(100 * progress / frames))
        if percent > last["percent"]:
            last["percent"] = percent
            emit("progress", percent=percent)

    port = job["port"]
    identity = usb_identity(port)
    if job.get("touch", True):
        emit("stage", stage="bootloader", text="Putting the radio into its bootloader")
        touch(port)
        time.sleep(1.5)
    port = find_port(port, identity)
    emit("log", text=f"Bootloader on {port}")

    for attempt in (1, 2):
        emit("stage", stage="writing", text="Writing the firmware")
        last["percent"] = -1
        transport = DfuTransportSerial(port, NRF_DFU_BAUD, False, single_bank=True, touch=0)
        transport.register_events_callback(DfuEvent.PROGRESS_EVENT, on_progress)
        try:
            Dfu(job["file"], dfu_transport=transport).dfu_send_images()
            break
        except Exception as exc:
            with contextlib.suppress(Exception):
                if transport.is_open():
                    transport.close()
            if attempt == 2:
                raise FlashError(
                    f"The update didn't go through ({exc}). The radio stays in its bootloader; "
                    "try again, or double-press its reset button and try again"
                ) from None
            emit("log", text=f"Transfer failed ({exc}); trying again")
            time.sleep(2)
            port = find_port(port, identity)
    emit("progress", percent=100)
    emit("stage", stage="restarting", text="Restarting the radio")


# --- Entry point -----------------------------------------------------------------------------


ACTIONS = {
    ("flash", "esp32"): flash_esp32,
    ("flash", "nrf52"): flash_nrf52,
    ("reset", "esp32"): reset_esp32,
}


def run(job: dict[str, Any], emit: Emit) -> bool:
    handler = ACTIONS.get((job.get("action"), job.get("platform")))
    if handler is None:
        emit("result", ok=False, error=f"Can't {job.get('action')} {job.get('platform')} radios")
        return False
    try:
        handler(job, emit)
    except (FlashError, FirmwareImageError) as exc:
        emit("result", ok=False, error=str(exc))
        return False
    except ImportError as exc:
        emit("result", ok=False, error=f"A flashing tool is missing ({exc.name}); install requirements.txt again")
        return False
    except Exception as exc:  # anything the tools raise
        emit("result", ok=False, error=f"{type(exc).__name__}: {exc}")
        return False
    emit("result", ok=True)
    return True


def main() -> int:
    # Progress goes to the real stdout; whatever the libraries print, to stderr.
    out = os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1, encoding="utf-8")
    sys.stdout = sys.stderr

    def emit(event: str, **fields: Any) -> None:
        out.write(json.dumps({"event": event, **fields}) + "\n")

    class Forward(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            text = record.getMessage().strip()
            if text:
                emit("log", text=text)

    logging.basicConfig(level=logging.INFO, handlers=[Forward()])
    try:
        job = json.loads(sys.stdin.read())
        if not isinstance(job, dict):
            raise ValueError("the job must be a JSON object")
    except ValueError as exc:
        emit("result", ok=False, error=f"Bad job: {exc}")
        return 2
    return 0 if run(job, emit) else 1


if __name__ == "__main__":
    sys.exit(main())
