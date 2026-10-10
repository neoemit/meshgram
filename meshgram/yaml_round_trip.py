"""Editing config.yaml text while keeping its comments, quoting and layout.

Meshgram reads config.yaml with PyYAML (YAML 1.1); these helpers write it with
ruamel.yaml's round-trip mode, quoting new strings so PyYAML reads them back
unchanged. Shared by ``migrate_config`` and ``config_export``.
"""

from __future__ import annotations

from typing import Any

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import DoubleQuotedScalarString, ScalarString
from ruamel.yaml.util import load_yaml_guess_indent

MISSING: Any = object()


def plain_is_safe(value: str) -> bool:
    """Whether a YAML 1.1 reader (PyYAML) reads ``value`` back unquoted as the same string."""
    try:
        return yaml.safe_load(value) == value
    except yaml.YAMLError:
        return False


def yaml_value(value: Any, old: Any = MISSING) -> Any:
    """``value`` ready to go where ``old`` was (``MISSING`` for a new key)."""
    if not isinstance(value, str):
        return value
    if isinstance(old, ScalarString):
        return type(old)(value)  # Keep the quoting style already in the file.
    # Quote what PyYAML would take for something else, like "on", "12:30" or "0x1f".
    return value if plain_is_safe(value) else DoubleQuotedScalarString(value)


def same(old: Any, new: Any) -> bool:
    """Whether ``old`` (``MISSING`` when absent) already is ``new``, so its formatting can stay."""
    # Keep True apart from 1, which compare equal in Python.
    return old is not MISSING and isinstance(old, bool) == isinstance(new, bool) and old == new


def _guess_mapping_indent(text: str) -> int:
    # The first indented key; ruamel's guess only covers sequences.
    for line in text.splitlines():
        stripped = line.lstrip(" ")
        if stripped and len(stripped) < len(line) and not stripped.startswith(("#", "-")):
            return len(line) - len(stripped)
    return 2


def round_trip_yaml(text: str) -> YAML:
    """A YAML loader/dumper that keeps comments, quoting and the indentation of ``text``."""
    mapping_indent = _guess_mapping_indent(text)
    _, sequence_indent, sequence_offset = load_yaml_guess_indent(text)
    round_trip = YAML()
    round_trip.preserve_quotes = True
    round_trip.width = 4096
    round_trip.indent(
        mapping=mapping_indent,
        sequence=sequence_indent or mapping_indent,
        offset=sequence_offset or 0,
    )
    return round_trip
