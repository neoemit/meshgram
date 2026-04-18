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

    # Keep refining chunks until prefix-aware limits converge.
    raw_chunks: list[str] = [text]
    max_iterations = 128

    for _ in range(max_iterations):
        total = len(raw_chunks)
        next_raw_chunks: list[str] = []
        changed = False

        for index, raw_chunk in enumerate(raw_chunks, start=1):
            prefix = prefix_template.format(index=index, total=total)
            available = payload_limit - utf8_len(prefix)
            if available <= 0:
                raise ValueError("Chunk prefix leaves no space for payload")

            split_parts = split_text_by_bytes(raw_chunk, available)
            if not split_parts:
                continue

            if len(split_parts) != 1 or split_parts[0] != raw_chunk:
                changed = True
            next_raw_chunks.extend(split_parts)

        if not changed and len(next_raw_chunks) == len(raw_chunks):
            break
        raw_chunks = next_raw_chunks
    else:
        raise ValueError("Chunking did not converge for payload limit")

    if len(raw_chunks) == 1:
        return raw_chunks

    total = len(raw_chunks)
    result: list[str] = []
    for index, raw_chunk in enumerate(raw_chunks, start=1):
        prefix = prefix_template.format(index=index, total=total)
        combined = f"{prefix}{raw_chunk}"
        if utf8_len(combined) > payload_limit:
            raise ValueError("Chunk exceeds payload limit after convergence")
        result.append(combined)

    return result


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
