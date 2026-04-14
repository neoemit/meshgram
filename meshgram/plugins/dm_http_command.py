from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Optional
from urllib import request

from meshgram.plugin import BasePlugin
from meshgram.text_utils import normalized_exact_word
from meshgram.types import MeshtasticTextEvent, PluginAction, PluginContext, SendMeshtasticAction

LOGGER = logging.getLogger(__name__)
ENV_TEMPLATE_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class DirectMessageHttpCommandPlugin(BasePlugin):
    name = "dm_http_command"

    async def on_meshtastic_message(
        self,
        event: MeshtasticTextEvent,
        context: PluginContext,
    ) -> list[PluginAction]:
        if not _is_direct_message_to_local_node(event, context):
            return []

        raw_text = event.text.strip()
        if not raw_text or any(char.isspace() for char in raw_text):
            return []

        command_key = normalized_exact_word(raw_text)
        if not command_key:
            return []

        command_config = self._command_map().get(command_key)
        if command_config is None:
            return []

        try:
            response_text = await self._execute_command(command_key, command_config)
        except Exception as exc:
            LOGGER.warning("dm_http_command failed for command %s: %s", raw_text, exc)
            response_text = _safe_format_message(
                str(self.settings.get("error_message", "Request failed")),
                value="",
                command=raw_text,
            )
            if not response_text.strip():
                return []

        return [
            SendMeshtasticAction(
                text=response_text,
                destination_id=event.from_id,
                channel_index=event.channel_index,
                reply_id=event.packet_id,
            )
        ]

    def _command_map(self) -> dict[str, dict[str, Any]]:
        configured = self.settings.get("commands")
        if not isinstance(configured, dict):
            return {}

        parsed: dict[str, dict[str, Any]] = {}
        for raw_command, raw_config in configured.items():
            if not isinstance(raw_config, dict):
                continue

            command_key = normalized_exact_word(str(raw_command))
            if not command_key:
                continue

            url = str(raw_config.get("url", "")).strip()
            if not url:
                continue

            response_type = str(raw_config.get("type", "json")).strip().lower() or "json"
            value_path = raw_config.get("value")
            message_template = str(raw_config.get("msg", "{value}"))
            timeout_seconds = _as_positive_float(
                raw_config.get("timeout_seconds"),
                _as_positive_float(self.settings.get("timeout_seconds"), 8.0),
            )

            raw_headers = raw_config.get("headers", {})
            headers: dict[str, str] = {}
            if isinstance(raw_headers, dict):
                for key, value in raw_headers.items():
                    header_key = str(key).strip()
                    header_value = str(value)
                    if header_key and header_value:
                        headers[header_key] = header_value

            auth_type = ""
            auth_token_env = ""
            auth_header = "Authorization"
            auth_prefix = "Bearer"
            raw_auth = raw_config.get("auth")
            if isinstance(raw_auth, dict):
                auth_type = str(raw_auth.get("type", "")).strip().lower()
                auth_token_env = str(raw_auth.get("token_env", raw_auth.get("env", ""))).strip()
                auth_header = str(raw_auth.get("header", "Authorization")).strip() or "Authorization"
                auth_prefix = str(raw_auth.get("prefix", "Bearer")).strip() or "Bearer"

            parsed[command_key] = {
                "url": url,
                "type": response_type,
                "value": value_path,
                "msg": message_template,
                "headers": headers,
                "timeout_seconds": timeout_seconds,
                "auth_type": auth_type,
                "auth_token_env": auth_token_env,
                "auth_header": auth_header,
                "auth_prefix": auth_prefix,
            }

        return parsed

    async def _execute_command(self, command: str, command_config: dict[str, Any]) -> str:
        url = _expand_env_templates(str(command_config["url"]))
        timeout_seconds = float(command_config["timeout_seconds"])
        headers = _resolve_headers(command_config.get("headers", {}))
        headers = _apply_auth(headers, command_config)
        response_type = str(command_config.get("type", "json")).lower()
        value_path = command_config.get("value")
        template = str(command_config.get("msg", "{value}"))

        payload_bytes = await asyncio.to_thread(self._http_get, url, timeout_seconds, headers)
        value = self._extract_value(payload_bytes, response_type, value_path)
        message = _safe_format_message(template, value=value, command=command)
        if not message.strip():
            raise ValueError("Formatted response message is empty")
        return message

    def _http_get(self, url: str, timeout_seconds: float, headers: dict[str, str]) -> bytes:
        req = request.Request(url=url, headers=headers)
        with request.urlopen(req, timeout=timeout_seconds) as response:
            return response.read()

    def _extract_value(self, payload: bytes, response_type: str, value_path: Any) -> Any:
        if response_type == "json":
            decoded = json.loads(payload.decode("utf-8"))
            return _resolve_path(decoded, value_path)
        if response_type == "text":
            decoded = payload.decode("utf-8", errors="ignore")
            if value_path is None:
                return decoded
            return _resolve_path(decoded, value_path)
        raise ValueError(f"Unsupported response type: {response_type}")


def _as_positive_float(value: Any, default: float) -> float:
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed <= 0:
        return default
    return parsed


def _safe_format_message(template: str, *, value: Any, command: str) -> str:
    try:
        return template.format(value=value, command=command)
    except (KeyError, ValueError):
        return str(value)


def _resolve_path(data: Any, path: Any) -> Any:
    if path is None:
        return data

    path_text = str(path).strip()
    if not path_text:
        return data

    current = data
    parts = path_text.split(".")
    for part in parts:
        if isinstance(current, dict):
            if part not in current:
                raise KeyError(f"Path not found: {path_text}")
            current = current[part]
            continue

        if isinstance(current, list):
            try:
                index = int(part)
            except ValueError as exc:
                raise KeyError(f"List index expected while resolving path: {path_text}") from exc
            current = current[index]
            continue

        raise KeyError(f"Cannot descend into path {path_text} at segment {part}")

    return current


def _normalize_node_id(node_id: Optional[str]) -> Optional[str]:
    if not node_id:
        return None
    text = node_id.strip().lower()
    if not text:
        return None
    if text.startswith("!"):
        return text
    if text.startswith("0x"):
        text = text[2:]
    if text and all(char in "0123456789abcdef" for char in text):
        return f"!{text}"
    return text


def _is_direct_message_to_local_node(event: MeshtasticTextEvent, context: PluginContext) -> bool:
    local_node_id = _normalize_node_id(context.local_node_id)
    to_id = _normalize_node_id(event.to_id)
    from_id = _normalize_node_id(event.from_id)

    if not local_node_id or not to_id or not from_id:
        return False

    return to_id == local_node_id


def _expand_env_templates(value: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        env_key = match.group(1)
        env_value = os.getenv(env_key)
        if env_value is None:
            raise KeyError(f"Missing environment variable: {env_key}")
        return env_value

    return ENV_TEMPLATE_PATTERN.sub(_replace, value)


def _resolve_headers(raw_headers: Any) -> dict[str, str]:
    headers: dict[str, str] = {}
    if not isinstance(raw_headers, dict):
        return headers

    for key, value in raw_headers.items():
        header_key = str(key).strip()
        if not header_key:
            continue
        header_value = _expand_env_templates(str(value))
        headers[header_key] = header_value

    return headers


def _apply_auth(headers: dict[str, str], command_config: dict[str, Any]) -> dict[str, str]:
    auth_type = str(command_config.get("auth_type", "")).strip().lower()
    if not auth_type:
        return headers
    if auth_type != "bearer":
        raise ValueError(f"Unsupported auth type: {auth_type}")

    token_env = str(command_config.get("auth_token_env", "")).strip()
    if not token_env:
        raise ValueError("Bearer auth requires token_env")

    token = os.getenv(token_env)
    if not token:
        raise ValueError(f"Bearer token env var is missing/empty: {token_env}")

    header_name = str(command_config.get("auth_header", "Authorization")).strip() or "Authorization"
    prefix = str(command_config.get("auth_prefix", "Bearer")).strip() or "Bearer"
    auth_value = f"{prefix} {token}"

    result = dict(headers)
    result.setdefault(header_name, auth_value)
    return result
