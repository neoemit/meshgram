"""Plugin settings schemas: a JSON Schema subset, validation and secret masking.

Plugins describe their settings with JSON Schema (draft 2020-12 keywords) so
the web app can render a form for them and check what it sends back. Only the
keywords used here are understood:

``type`` (one or a list), ``properties``, ``additionalProperties``,
``required``, ``items``, ``enum``, ``minimum``, ``maximum``,
``exclusiveMinimum``, ``minLength``, ``maxLength``, ``pattern``, ``minItems``,
``uniqueItems``; plus the annotations ``title``, ``description``, ``default``
and ``writeOnly``.

``writeOnly: true`` marks a secret: the web app gets ``SECRET_MASK`` in its
place, and sending the mask back keeps the stored value.

Settings in config.yaml aren't validated (the plugins already accept loose
values there, like ``"0,1"`` for a list of channels); only edits from the web
app are.
"""
from __future__ import annotations

import re
from typing import Any

SECRET_MASK = "••••••••"

_TYPE_CHECKS = {
    "object": lambda value: isinstance(value, dict),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    # bool is an int in Python, but not in JSON.
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
}


class SettingsError(ValueError):
    """Settings that don't match their schema; ``errors`` lists ``(path, message)``."""

    def __init__(self, errors: list[tuple[str, str]]):
        self.errors = errors
        super().__init__("; ".join(f"{path or 'settings'}: {message}" for path, message in errors))


def _join(path: str, key: Any) -> str:
    return f"{path}.{key}" if path else str(key)


def _types(schema: dict[str, Any]) -> list[str]:
    declared = schema.get("type")
    if declared is None:
        return []
    return [declared] if isinstance(declared, str) else list(declared)


def _validate(value: Any, schema: dict[str, Any], path: str, errors: list[tuple[str, str]]) -> None:
    types = _types(schema)
    if types and not any(_TYPE_CHECKS[name](value) for name in types):
        errors.append((path, f"must be {' or '.join(types)}"))
        return

    if "enum" in schema and value not in schema["enum"]:
        errors.append((path, f"must be one of {', '.join(map(str, schema['enum']))}"))
        return

    if _TYPE_CHECKS["number"](value):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append((path, f"must be at least {schema['minimum']}"))
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append((path, f"must be more than {schema['exclusiveMinimum']}"))
        if "maximum" in schema and value > schema["maximum"]:
            errors.append((path, f"must be at most {schema['maximum']}"))

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append((path, "must not be empty" if schema["minLength"] == 1 else f"must be at least {schema['minLength']} characters"))
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append((path, f"must be at most {schema['maxLength']} characters"))
        if "pattern" in schema and value and not re.search(schema["pattern"], value):
            errors.append((path, schema.get("x-pattern-message") or "has the wrong format"))

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append((path, f"needs at least {schema['minItems']} items"))
        if schema.get("uniqueItems") and len({repr(item) for item in value}) != len(value):
            errors.append((path, "must not repeat items"))
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate(item, item_schema, _join(path, index), errors)

    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in value:
                errors.append((_join(path, key), "is required"))
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in properties:
                _validate(item, properties[key], _join(path, key), errors)
            elif extra is False:
                errors.append((_join(path, key), "isn't a known setting"))
            elif isinstance(extra, dict):
                if not str(key).strip():
                    errors.append((path, "keys must not be empty"))
                _validate(item, extra, _join(path, key), errors)


def validate(value: Any, schema: dict[str, Any]) -> None:
    """Raise ``SettingsError`` unless ``value`` matches ``schema``."""
    errors: list[tuple[str, str]] = []
    _validate(value, schema, "", errors)
    if errors:
        raise SettingsError(errors)


def _child_schema(schema: dict[str, Any], key: Any) -> dict[str, Any] | None:
    if isinstance(key, int):
        items = schema.get("items")
        return items if isinstance(items, dict) else None
    properties = schema.get("properties") or {}
    if key in properties:
        return properties[key]
    extra = schema.get("additionalProperties")
    return extra if isinstance(extra, dict) else None


def mask_secrets(value: Any, schema: dict[str, Any] | None) -> Any:
    """A copy of ``value`` with every set ``writeOnly`` value replaced by ``SECRET_MASK``."""
    if not isinstance(schema, dict):
        return value
    if schema.get("writeOnly"):
        return SECRET_MASK if value not in (None, "") else value
    if isinstance(value, dict):
        return {key: mask_secrets(item, _child_schema(schema, key)) for key, item in value.items()}
    if isinstance(value, list):
        return [mask_secrets(item, _child_schema(schema, index)) for index, item in enumerate(value)]
    return value


def restore_secrets(value: Any, previous: Any, schema: dict[str, Any] | None, path: str = "") -> Any:
    """Put the stored secrets back where ``value`` still holds ``SECRET_MASK``.

    A mask with nothing stored at the same place (a renamed entry, say) can't
    be resolved and raises ``SettingsError``: the secret has to be typed again.
    """
    if not isinstance(schema, dict):
        return value
    if schema.get("writeOnly") and value == SECRET_MASK:
        if previous in (None, "", SECRET_MASK):
            raise SettingsError([(path, "enter the secret again")])
        return previous
    if isinstance(value, dict):
        old = previous if isinstance(previous, dict) else {}
        return {
            key: restore_secrets(item, old.get(key), _child_schema(schema, key), _join(path, key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        old_list = previous if isinstance(previous, list) else []
        return [
            restore_secrets(item, old_list[index] if index < len(old_list) else None, _child_schema(schema, index), _join(path, index))
            for index, item in enumerate(value)
        ]
    return value
