"""Decoding helpers for raw MeshCore RF packets (as delivered by RX_LOG_DATA).

Only the unencrypted parts of a packet are decoded: the header, transport codes,
repeater path and the cleartext prefix of each payload type (advert contents,
source/destination hashes, channel hash, ...). Encrypted bodies stay opaque.
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Any, Iterable, Optional

ROUTE_TYPE_TRANSPORT_FLOOD = 0x00
ROUTE_TYPE_FLOOD = 0x01
ROUTE_TYPE_DIRECT = 0x02
ROUTE_TYPE_TRANSPORT_DIRECT = 0x03
DIRECT_ROUTE_TYPES = {ROUTE_TYPE_DIRECT, ROUTE_TYPE_TRANSPORT_DIRECT}

PAYLOAD_TYPE_REQ = 0
PAYLOAD_TYPE_RESPONSE = 1
PAYLOAD_TYPE_TXT_MSG = 2
PAYLOAD_TYPE_ACK = 3
PAYLOAD_TYPE_ADVERT = 4
PAYLOAD_TYPE_GRP_TXT = 5
PAYLOAD_TYPE_GRP_DATA = 6
PAYLOAD_TYPE_ANON_REQ = 7
PAYLOAD_TYPE_PATH = 8
PAYLOAD_TYPE_TRACE = 9

ROUTE_TYPE_NAMES = {
    ROUTE_TYPE_TRANSPORT_FLOOD: "TRANSPORT_FLOOD",
    ROUTE_TYPE_FLOOD: "FLOOD",
    ROUTE_TYPE_DIRECT: "DIRECT",
    ROUTE_TYPE_TRANSPORT_DIRECT: "TRANSPORT_DIRECT",
}

PAYLOAD_TYPE_NAMES = {
    PAYLOAD_TYPE_REQ: "REQ",
    PAYLOAD_TYPE_RESPONSE: "RESPONSE",
    PAYLOAD_TYPE_TXT_MSG: "TXT_MSG",
    PAYLOAD_TYPE_ACK: "ACK",
    PAYLOAD_TYPE_ADVERT: "ADVERT",
    PAYLOAD_TYPE_GRP_TXT: "GRP_TXT",
    PAYLOAD_TYPE_GRP_DATA: "GRP_DATA",
    PAYLOAD_TYPE_ANON_REQ: "ANON_REQ",
    PAYLOAD_TYPE_PATH: "PATH",
    PAYLOAD_TYPE_TRACE: "TRACE",
    10: "MULTIPART",
    11: "CONTROL",
    15: "RAW_CUSTOM",
}

# Advert "flags" low nibble.
NODE_TYPE_NAMES = {1: "chat", 2: "repeater", 3: "room", 4: "sensor"}

ADVERT_FLAG_HAS_LOCATION = 0x10
ADVERT_FLAG_HAS_FEATURE1 = 0x20
ADVERT_FLAG_HAS_FEATURE2 = 0x40
ADVERT_FLAG_HAS_NAME = 0x80

# Payload types whose cleartext prefix is ``dest_hash(1) || src_hash(1)``.
_ADDRESSED_PAYLOAD_TYPES = {PAYLOAD_TYPE_REQ, PAYLOAD_TYPE_RESPONSE, PAYLOAD_TYPE_TXT_MSG, PAYLOAD_TYPE_PATH}


def parse_packet(raw: bytes) -> Optional[dict[str, Any]]:
    """Decode the MeshCore packet header. Returns ``None`` for truncated packets."""
    if len(raw) < 2:
        return None
    header = raw[0]
    route_type = header & 0x03
    payload_type = (header >> 2) & 0x0F
    offset = 1
    transport_codes: Optional[bytes] = None
    if route_type in (ROUTE_TYPE_TRANSPORT_FLOOD, ROUTE_TYPE_TRANSPORT_DIRECT):
        transport_codes = raw[offset : offset + 4]  # two 16-bit transport codes
        offset += 4
    if len(raw) <= offset:
        return None
    path_len_byte = raw[offset]
    offset += 1
    hash_size = (path_len_byte >> 6) + 1
    hop_count = path_len_byte & 0x3F
    path_end = offset + hop_count * hash_size
    if path_end > len(raw):
        return None
    path = raw[offset:path_end]
    return {
        "header": header,
        "route_type": route_type,
        "payload_type": payload_type,
        "payload_version": header >> 6,
        "transport_codes": transport_codes,
        "path_len_byte": path_len_byte,
        "path_hash_size": hash_size,
        "path_hashes": [path[i : i + hash_size].hex().upper() for i in range(0, len(path), hash_size)],
        "payload": raw[path_end:],
    }


def packet_hash(payload_type: int, path_len_byte: int, payload: bytes) -> str:
    """Same as MeshCore ``Packet::calculatePacketHash`` (first 8 bytes of SHA-256)."""
    digest = hashlib.sha256()
    digest.update(bytes([payload_type]))
    if payload_type == PAYLOAD_TYPE_TRACE:
        digest.update(path_len_byte.to_bytes(2, "little"))
    digest.update(payload)
    return digest.hexdigest()[:16].upper()


def decode_advert(payload: bytes) -> Optional[dict[str, Any]]:
    """Decode an ADVERT payload: public key, timestamp, node type, location, name."""
    if len(payload) < 101:
        return None
    public_key = payload[0:32]
    timestamp = int.from_bytes(payload[32:36], "little")
    flags = payload[100]
    offset = 101
    advert: dict[str, Any] = {
        "public_key": public_key.hex().upper(),
        "advert_timestamp": timestamp,
        "node_type": NODE_TYPE_NAMES.get(flags & 0x0F, "unknown"),
    }
    if flags & ADVERT_FLAG_HAS_LOCATION:
        if len(payload) < offset + 8:
            return advert
        lat = int.from_bytes(payload[offset : offset + 4], "little", signed=True) / 1_000_000
        lon = int.from_bytes(payload[offset + 4 : offset + 8], "little", signed=True) / 1_000_000
        offset += 8
        if is_valid_position(lat, lon):
            advert["lat"] = lat
            advert["lon"] = lon
    if flags & ADVERT_FLAG_HAS_FEATURE1:
        offset += 2
    if flags & ADVERT_FLAG_HAS_FEATURE2:
        offset += 2
    if flags & ADVERT_FLAG_HAS_NAME and offset < len(payload):
        name = payload[offset:].split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
        if name:
            advert["name"] = name
    return advert


def decrypt_group_text(payload: bytes, channels: Iterable[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Decrypt a GRP_TXT payload with the first matching channel secret, like meshcore_py does.

    The payload is ``channel_hash(1) || MAC(2) || AES-128-ECB ciphertext``; the MAC is the start of
    HMAC-SHA256(secret, ciphertext) and the plaintext is ``timestamp(4) || flags(1) || text``, where
    the text is ``"<sender>: <message>"``. ``channels`` items have ``name``, ``secret`` (16 bytes)
    and ``hash`` (hex of the first byte of SHA-256(secret)). Returns ``None`` if no channel matches.
    """
    if len(payload) < 3 + 16 or (len(payload) - 3) % 16:
        return None
    try:
        from Crypto.Cipher import AES  # pycryptodome, a meshcore_py dependency
    except ImportError:  # pragma: no cover - only without meshcore_py installed
        return None
    channel_hash = payload[0:1].hex()
    mac = payload[1:3]
    ciphertext = payload[3:]
    for channel in channels:
        secret = channel.get("secret")
        if not isinstance(secret, (bytes, bytearray)) or str(channel.get("hash") or "").lower() != channel_hash:
            continue
        if hmac.new(bytes(secret), ciphertext, hashlib.sha256).digest()[:2] != mac:
            continue
        plaintext = AES.new(bytes(secret), AES.MODE_ECB).decrypt(ciphertext)
        return {
            "channel_name": str(channel.get("name") or ""),
            "message": plaintext[5:].strip(b"\0").decode("utf-8", "ignore"),
            "sender_timestamp": int.from_bytes(plaintext[0:4], "little"),
        }
    return None


def is_valid_position(lat: Any, lon: Any) -> bool:
    """MeshCore uses 0,0 for "no location"; also reject out-of-range values."""
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return False
    if lat_f == 0 and lon_f == 0:
        return False
    return -90 <= lat_f <= 90 and -180 <= lon_f <= 180


def decode_packet(raw: bytes) -> Optional[dict[str, Any]]:
    """Decode everything readable without keys. Returns ``None`` for truncated packets."""
    parsed = parse_packet(raw)
    if parsed is None:
        return None

    payload_type = parsed["payload_type"]
    route_type = parsed["route_type"]
    payload: bytes = parsed["payload"]
    decoded: dict[str, Any] = {
        "hash": packet_hash(payload_type, parsed["path_len_byte"], payload),
        "len": len(raw),
        "payload_len": len(payload),
        "payload_type": payload_type,
        "payload_type_name": PAYLOAD_TYPE_NAMES.get(payload_type, f"TYPE_{payload_type}"),
        "payload_version": parsed["payload_version"],
        "route_type": route_type,
        "route_type_name": ROUTE_TYPE_NAMES[route_type],
        "route": "direct" if route_type in DIRECT_ROUTE_TYPES else "flood",
        "path": parsed["path_hashes"],
        "path_hash_size": parsed["path_hash_size"],
        "hops": len(parsed["path_hashes"]),
    }
    if parsed["transport_codes"] is not None:
        codes = parsed["transport_codes"]
        decoded["transport_codes"] = [codes[0:2].hex().upper(), codes[2:4].hex().upper()]

    if payload_type == PAYLOAD_TYPE_ADVERT:
        advert = decode_advert(payload)
        if advert is not None:
            decoded["advert"] = advert
    elif payload_type in _ADDRESSED_PAYLOAD_TYPES and len(payload) >= 2:
        decoded["dest_hash"] = payload[0:1].hex().upper()
        decoded["src_hash"] = payload[1:2].hex().upper()
    elif payload_type == PAYLOAD_TYPE_ANON_REQ and len(payload) >= 33:
        decoded["dest_hash"] = payload[0:1].hex().upper()
        decoded["src_public_key"] = payload[1:33].hex().upper()
    elif payload_type in (PAYLOAD_TYPE_GRP_TXT, PAYLOAD_TYPE_GRP_DATA) and len(payload) >= 1:
        decoded["channel_hash"] = payload[0:1].hex().upper()
    elif payload_type == PAYLOAD_TYPE_ACK and len(payload) >= 4:
        decoded["ack_crc"] = payload[0:4].hex().upper()
    elif payload_type == PAYLOAD_TYPE_TRACE and len(payload) >= 4:
        decoded["trace_tag"] = payload[0:4].hex().upper()
        # In TRACE packets the "path" carries one SNR byte (x4, signed) per hop so far,
        # not repeater hashes, so it can't be drawn as a route.
        decoded["trace_snrs"] = [
            int.from_bytes(bytes.fromhex(value), "little", signed=True) / 4 for value in parsed["path_hashes"]
        ]
        decoded["path"] = []
    return decoded
