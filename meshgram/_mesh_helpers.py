from __future__ import annotations

import contextlib
from typing import Any, Optional

from meshtastic import BROADCAST_NUM

try:
    from meshtastic.protobuf import portnums_pb2
except ModuleNotFoundError:
    try:
        import meshtastic.portnums_pb2 as portnums_pb2  # type: ignore[attr-defined]
    except ModuleNotFoundError:
        portnums_pb2 = None  # type: ignore[assignment]


_EMOJI_MODIFIER_CODEPOINTS = {
    0x20E3,  # COMBINING ENCLOSING KEYCAP
    0xFE0E,  # VARIATION SELECTOR-15
    0xFE0F,  # VARIATION SELECTOR-16
}


def extract_optional_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        with contextlib.suppress(ValueError):
            return int(stripped)
        with contextlib.suppress(ValueError):
            return int(stripped, 0)
    return None


def node_num_to_id(node_num: int) -> str:
    return f"!{node_num & 0xFFFFFFFF:08x}"


def normalize_node_id(value: Any, fallback_num: Optional[int] = None) -> Optional[str]:
    if isinstance(value, str):
        text = value.strip()
        if text:
            if text.startswith("!"):
                text = text[1:]
            if text.lower().startswith("0x"):
                text = text[2:]

            if text.isdigit():
                if len(text) == 8:
                    return f"!{text.lower()}"
                return node_num_to_id(int(text))

            if text and all(char in "0123456789abcdefABCDEF" for char in text):
                return f"!{text.lower()}"

            return value.strip()

    if fallback_num is not None:
        return node_num_to_id(fallback_num)

    return None


def is_broadcast_destination(destination_id: Any) -> bool:
    if destination_id is None:
        return True

    if isinstance(destination_id, int):
        return destination_id == BROADCAST_NUM

    if isinstance(destination_id, str):
        normalized = destination_id.strip().lower()
        if not normalized:
            return False
        return normalized in {"^all", "all", "broadcast", "broadcast_addr"}

    return False


def is_text_message_portnum(portnum: Any) -> bool:
    if portnum == "TEXT_MESSAGE_APP":
        return True

    text_portnum_value: Optional[int]
    if portnums_pb2 is not None:
        text_portnum_value = int(portnums_pb2.PortNum.TEXT_MESSAGE_APP)
    else:
        # Meshtastic TEXT_MESSAGE_APP is historically enum value 1.
        text_portnum_value = 1

    if isinstance(portnum, int):
        return portnum == text_portnum_value

    if isinstance(portnum, str):
        normalized = portnum.strip()
        if not normalized:
            return False
        if normalized == "TEXT_MESSAGE_APP":
            return True
        maybe_int = extract_optional_int(normalized)
        return maybe_int == text_portnum_value

    return False


def is_emoji_modifier_codepoint(codepoint: int) -> bool:
    if codepoint in _EMOJI_MODIFIER_CODEPOINTS:
        return True
    if 0x1F3FB <= codepoint <= 0x1F3FF:
        return True
    return False


def is_emoji_modifier(char: str) -> bool:
    return is_emoji_modifier_codepoint(ord(char))


def sanitize_reaction_emoji_text(raw_text: str) -> Optional[str]:
    text = raw_text.strip()
    if not text:
        return None

    chars = list(text)
    start_index = 0
    while start_index < len(chars) and is_emoji_modifier(chars[start_index]):
        start_index += 1

    if start_index >= len(chars):
        return None

    first_visible = chars[start_index]
    emoji_chars: list[str] = [first_visible]
    index = start_index + 1
    while index < len(chars):
        char = chars[index]
        codepoint = ord(char)
        if is_emoji_modifier_codepoint(codepoint):
            emoji_chars.append(char)
            index += 1
            continue

        if char == "‍":
            emoji_chars.append(char)
            index += 1
            if index < len(chars):
                emoji_chars.append(chars[index])
                index += 1
            continue

        break

    normalized = "".join(emoji_chars).strip()
    if not normalized:
        return None

    if all(is_emoji_modifier(char) for char in normalized):
        return None

    return normalized


def extract_reaction_emoji_from_codepoint(codepoint: int) -> Optional[str]:
    if codepoint <= 0:
        return None
    if is_emoji_modifier_codepoint(codepoint):
        return None
    with contextlib.suppress(ValueError):
        return chr(codepoint)
    return None


def extract_reaction_emoji_from_value(raw_emoji: Any) -> Optional[str]:
    emoji_codepoint = extract_optional_int(raw_emoji)
    if emoji_codepoint is not None:
        return extract_reaction_emoji_from_codepoint(emoji_codepoint)

    if isinstance(raw_emoji, str):
        return sanitize_reaction_emoji_text(raw_emoji)

    if isinstance(raw_emoji, bytes):
        decoded_emoji = raw_emoji.decode("utf-8", errors="ignore")
        return sanitize_reaction_emoji_text(decoded_emoji)

    return None


def extract_reaction_emoji_from_payload(raw_payload: Any) -> Optional[str]:
    payload_text: Optional[str] = None
    if isinstance(raw_payload, bytes):
        payload_text = raw_payload.decode("utf-8", errors="ignore")
    elif isinstance(raw_payload, str):
        payload_text = raw_payload

    if not payload_text:
        return None

    return sanitize_reaction_emoji_text(payload_text)


def extract_reaction_emoji(decoded: dict[str, Any]) -> Optional[str]:
    if "emoji" not in decoded:
        return None

    raw_emoji = decoded.get("emoji")
    normalized = extract_reaction_emoji_from_value(raw_emoji)
    if normalized:
        return normalized

    # Some firmware/library paths expose only emoji modifiers in `decoded.emoji`
    # while carrying the visible glyph in payload. Use payload as best-effort fallback.
    return extract_reaction_emoji_from_payload(decoded.get("payload"))


def extract_meshtastic_packet_id(sent_packet: Any) -> Optional[int]:
    if sent_packet is None:
        return None

    if isinstance(sent_packet, dict):
        return extract_optional_int(sent_packet.get("id"))

    value = getattr(sent_packet, "id", None)
    return extract_optional_int(value)
