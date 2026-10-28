"""Login sessions for the web app: a signed cookie that lasts 30 days.

With ``web.password`` set, the page shows a login form; the password gets a
session cookie (``SESSION_SECONDS`` long, not renewed) instead of the browser's
own HTTP Basic prompt, which forgets the password when the browser closes.

Sessions are stateless: the cookie holds when it was issued and an HMAC of that
and the password, under a random key kept in the data directory
(``web_session.json``), so a restart keeps everyone logged in and changing the
password ends every session.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Optional

from ..storage import write_private_json

LOGGER = logging.getLogger(__name__)

SESSION_DAYS = 30
SESSION_SECONDS = SESSION_DAYS * 24 * 3600
KEY_FILE = "web_session.json"
# Clock differences: a session issued "in the future" by this much still counts.
_CLOCK_SLACK_SECONDS = 300


def load_session_key(path: Path) -> bytes:
    """The signing key in ``path``, made (and saved) on first use.

    If it can't be saved, a key for this run only: sessions then end on restart.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict):
        try:
            key = bytes.fromhex(str(data.get("key", "")))
        except ValueError:
            key = b""
        if len(key) >= 32:
            return key
    key = secrets.token_bytes(32)
    try:
        write_private_json(path, {"key": key.hex()})
    except OSError as exc:
        LOGGER.warning("Web app: can't save the login session key to %s (%s); logins end when Meshgram restarts", path, exc)
    return key


class SessionSigner:
    def __init__(self, key: bytes, password: str):
        self._key = key
        # The password is part of what's signed: a new one ends every session.
        self._password_digest = hashlib.sha256(password.encode("utf-8")).hexdigest()

    def _mac(self, issued: int) -> str:
        message = f"v1|{issued}|{self._password_digest}".encode("ascii")
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()

    def issue(self, now: Optional[float] = None) -> str:
        issued = int(time.time() if now is None else now)
        return f"{issued}.{self._mac(issued)}"

    def is_valid(self, token: str, now: Optional[float] = None) -> bool:
        issued_text, sep, mac = token.partition(".")
        if not sep or not (issued_text.isascii() and issued_text.isdigit()) or len(issued_text) > 12:
            return False
        if not hmac.compare_digest(mac.encode("ascii", "replace"), self._mac(int(issued_text)).encode("ascii")):
            return False
        age = (time.time() if now is None else now) - int(issued_text)
        return -_CLOCK_SLACK_SECONDS <= age < SESSION_SECONDS
