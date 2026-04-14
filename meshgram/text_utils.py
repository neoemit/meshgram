from __future__ import annotations

import re
import unicodedata


def utf8_len(value: str) -> int:
    return len(value.encode("utf-8"))


def split_text_by_bytes(text: str, max_bytes: int) -> list[str]:
    if max_bytes <= 0:
        raise ValueError("max_bytes must be > 0")

    if utf8_len(text) <= max_bytes:
        return [text]

    tokens = re.findall(r"\S+\s*|\s+", text)
    if not tokens:
        return []

    chunks: list[str] = []
    current = ""

    for token in tokens:
        candidate = current + token
        if candidate and utf8_len(candidate) <= max_bytes:
            current = candidate
            continue

        if current:
            trimmed = current.rstrip()
            if trimmed:
                chunks.append(trimmed)
            current = ""

        token = token.lstrip()
        if not token:
            continue

        if utf8_len(token) <= max_bytes:
            current = token
            continue

        hard_parts = _hard_split_by_bytes(token, max_bytes)
        if hard_parts:
            chunks.extend(hard_parts[:-1])
            current = hard_parts[-1]

    if current:
        trimmed = current.rstrip()
        if trimmed:
            chunks.append(trimmed)

    return chunks


def _hard_split_by_bytes(text: str, max_bytes: int) -> list[str]:
    parts: list[str] = []
    current = ""

    for char in text:
        candidate = current + char
        if utf8_len(candidate) <= max_bytes:
            current = candidate
            continue

        if current:
            parts.append(current)
            current = char
        else:
            parts.append(char)
            current = ""

    if current:
        parts.append(current)

    return parts


def split_for_meshtastic(
    text: str,
    payload_limit: int,
    prefix_template: str,
    chunking_enabled: bool,
) -> list[str]:
    if not text:
        return []

    if payload_limit <= 0:
        raise ValueError("payload_limit must be > 0")

    if not chunking_enabled:
        if utf8_len(text) > payload_limit:
            raise ValueError("Message exceeds Meshtastic payload limit while chunking is disabled")
        return [text]

    if utf8_len(text) <= payload_limit:
        return [text]

    chunk_count = 1
    while True:
        prefix_sample = prefix_template.format(index=chunk_count, total=chunk_count)
        available = payload_limit - utf8_len(prefix_sample)
        if available <= 0:
            raise ValueError("Chunk prefix leaves no space for payload")

        provisional_chunks = split_text_by_bytes(text, available)
        new_count = len(provisional_chunks)

        if new_count == chunk_count:
            chunks = provisional_chunks
            break

        chunk_count = new_count

    if len(chunks) == 1:
        return chunks

    final_chunks: list[str] = []
    total = len(chunks)
    for index, chunk in enumerate(chunks, start=1):
        prefix = prefix_template.format(index=index, total=total)
        combined = f"{prefix}{chunk}"
        if utf8_len(combined) > payload_limit:
            available = payload_limit - utf8_len(prefix)
            if available <= 0:
                raise ValueError("Chunk prefix leaves no space for payload")
            piece_chunks = split_text_by_bytes(chunk, available)
            for piece in piece_chunks:
                final_chunks.append(f"{prefix}{piece}")
            continue
        final_chunks.append(combined)

    return final_chunks


def _is_edge_noise(char: str) -> bool:
    category = unicodedata.category(char)
    return category.startswith("P") or category.startswith("S")


def strip_edge_noise(text: str) -> str:
    if not text:
        return ""

    start = 0
    end = len(text)

    while start < end and _is_edge_noise(text[start]):
        start += 1
    while end > start and _is_edge_noise(text[end - 1]):
        end -= 1

    return text[start:end]


def normalized_exact_word(text: str) -> str:
    trimmed = strip_edge_noise(text.strip())
    return trimmed.casefold()
