"""How MeshSOS boards read a channel message (a port of MeshSOS's SosProtocol.cpp).

A board takes every message on its channel as one of three things: an ACK ("ack", "ok",
"okay", "aware"), a RESOLVED ("resolved", "resolve", "safe") or, for any other text, an
alarm. A ``#xxxx`` token names the incident. ``<Name> text`` at the start means a gateway
sent it for Name. Keep this file in step with the firmware: both sides must agree.
"""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import Optional

ALARM = "alarm"
ACK = "ack"
RESOLVED = "resolved"

ACK_WORDS = frozenset({"ack", "ok", "okay", "aware"})
RESOLVED_WORDS = frozenset({"resolved", "resolve", "safe"})

MAX_NAME_LENGTH = 31
SOS_TEXT = "SOS! Need help."

_ID_TOKEN = re.compile(r"(?<![0-9A-Za-z])#([0-9A-Fa-f]{4})(?![0-9A-Za-z])")
_RELAYED = re.compile(r"^\s*<([^<>:\r\n]{1,%d})>(?:\s+|$)" % MAX_NAME_LENGTH)
_TRAILING_PUNCTUATION = re.compile(r"[^0-9A-Za-z]+$")


@dataclass(slots=True)
class SosMessage:
    sender: str
    # The node that relayed it, when a gateway sent it in the sender's name.
    via: Optional[str]
    body: str
    kind: str
    # The incident's id (four lower-case hex digits), when the text names one.
    incident_id: Optional[str]


def classify(body: str) -> Optional[str]:
    """ALARM, ACK or RESOLVED from the first word; None for an empty text."""
    words = body.split()
    if not words:
        return None
    word = _TRAILING_PUNCTUATION.sub("", words[0]).lower()
    if word in ACK_WORDS:
        return ACK
    if word in RESOLVED_WORDS:
        return RESOLVED
    return ALARM


def find_incident_id(body: str) -> Optional[str]:
    match = _ID_TOKEN.search(body)
    return match.group(1).lower() if match else None


def split_relayed(body: str) -> tuple[Optional[str], str]:
    """``("Anna", "ack")`` for "<Anna> ack"; ``(None, body)`` for anything else."""
    match = _RELAYED.match(body)
    if not match or not match.group(1).strip():
        return None, body
    return match.group(1).strip(), body[match.end():]


def parse(sender: str, text: str) -> Optional[SosMessage]:
    """What a board makes of ``text`` sent by ``sender``; None when there's nothing in it."""
    relayed_for, body = split_relayed(text)
    via = None
    if relayed_for is not None:
        via, sender = sender, relayed_for
    body = body.strip()
    kind = classify(body)
    if kind is None:
        return None
    return SosMessage(
        sender=" ".join(sender.split()) or "?",
        via=via,
        body=body,
        kind=kind,
        incident_id=find_incident_id(body),
    )


def display_text(body: str) -> str:
    """The text without its incident id, as a board shows it."""
    return " ".join(_ID_TOKEN.sub(" ", body).split())


def new_incident_id() -> str:
    return f"{secrets.randbelow(0x10000):04x}"


def relayed(name: str, text: str) -> str:
    """``text`` as a gateway sends it in ``name``'s name."""
    return f"<{name}> {text}"


def build_sos(incident_id: str) -> str:
    return f"{SOS_TEXT} #{incident_id}"


def build_ack(incident_id: Optional[str]) -> str:
    return f"ACK #{incident_id} I am aware" if incident_id else "ACK I am aware"


def build_resolved(incident_id: Optional[str]) -> str:
    return f"RESOLVED #{incident_id} all OK" if incident_id else "RESOLVED all OK"
