"""Merge the settings from ``.env`` into a single config file.

Older Meshgram versions took settings from two places, ``config.yaml`` and
environment variables (usually loaded from ``.env``), and an environment
variable won over the YAML value. Now every setting lives in ``config.yaml``.

This tool applies those old rules once and writes the result to a new file,
keeping the comments and layout of your ``config.yaml``::

    python -m meshgram.migrate_config        # writes config.migrated.yaml
    mv config.migrated.yaml config.yaml      # once it looks right

Like the old loader, it reads this process's environment as well as ``.env``,
and the environment wins; pass ``--ignore-environment`` to read ``.env`` only.
The output holds secrets, so only its owner can read it.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence, TextIO

import yaml
from dotenv import dotenv_values
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.scalarstring import DoubleQuotedScalarString, ScalarString
from ruamel.yaml.util import load_yaml_guess_indent

from .config import (
    CONFIG_PATH_ENV,
    DEFAULT_CONFIG_PATH,
    MESHCORE_BACKEND,
    MESHCORE_MODES,
    MESHTASTIC_BACKEND,
    MESHTASTIC_MODES,
    SUPPORTED_BACKENDS,
    ConfigError,
    LEGACY_ENV_VARS,
    build_settings,
)
from .plugins.dm_http_command import ENV_TEMPLATE_PATTERN

DEFAULT_ENV_FILE = ".env"
DEFAULT_OUTPUT = "config.migrated.yaml"
DATA_DIR_ENV = "MESHGRAM_DATA_DIR"
BACKEND_MODES = {MESHTASTIC_BACKEND: MESHTASTIC_MODES, MESHCORE_BACKEND: MESHCORE_MODES}
PLUGIN_SEGMENT = re.compile(r"plugins\[(\w+)\]")
STALE_COMMENT = re.compile(r"#.*(?:\.env\b|\b(?:%s)\b)" % "|".join(LEGACY_ENV_VARS))
MISSING: Any = object()


class MigrationError(Exception):
    """The inputs can't be migrated as they are."""


def _text(raw: str) -> str:
    return raw.strip()


def _lower(raw: str) -> str:
    return raw.strip().lower()


def _integer(raw: str) -> int:
    return int(raw.strip())


def _boolean(raw: str) -> bool:
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"not a boolean: {raw!r}")


@dataclass(frozen=True, slots=True)
class EnvSetting:
    # Dotted path in the config. "{backend}" is the active backend's section and
    # "plugins[name]" every entry of that plugin, as the old code applied them.
    path: str
    convert: Callable[[str], Any] = _text
    secret: bool = False


ENV_SETTINGS: dict[str, EnvSetting] = {
    "TELEGRAM_BOT_TOKEN": EnvSetting("telegram.bot_token", secret=True),
    "TELEGRAM_GROUP_ID": EnvSetting("telegram.group_id", _integer),
    "LOG_LEVEL": EnvSetting("runtime.log_level"),
    # Before the "{backend}" settings, which follow whatever it selects.
    "MESH_BACKEND": EnvSetting("mesh.backend", _lower),
    "MESH_MODE": EnvSetting("{backend}.connection.mode", _lower),
    "MESH_DEVICE": EnvSetting("{backend}.connection.serial_device"),
    "MESH_HOST": EnvSetting("{backend}.connection.tcp_host"),
    "MESH_PORT": EnvSetting("{backend}.connection.tcp_port", _integer),
    "MESH_NO_NODES": EnvSetting("meshtastic.connection.no_nodes", _boolean),
    "MESH_BAUDRATE": EnvSetting("meshcore.connection.baudrate", _integer),
    "MESH_BLE_ADDRESS": EnvSetting("meshcore.connection.ble_address"),
    "MESH_BLE_PIN": EnvSetting("meshcore.connection.ble_pin", secret=True),
    "MESH_AUTO_RECONNECT": EnvSetting("meshcore.connection.auto_reconnect", _boolean),
    "MESHMAPPER_IATA": EnvSetting("plugins[meshmapper].settings.iata"),
    "MESHMAPPER_PRIVATE_KEY": EnvSetting("plugins[meshmapper].settings.private_key", secret=True),
    "MESHMAPPER_SUBSCRIBE_USERNAME": EnvSetting("plugins[meshmapper].settings.subscribe_username"),
    "MESHMAPPER_SUBSCRIBE_PASSWORD": EnvSetting("plugins[meshmapper].settings.subscribe_password", secret=True),
    "PACKET_MAP_HOST": EnvSetting("plugins[packet_map].settings.host"),
    "PACKET_MAP_PORT": EnvSetting("plugins[packet_map].settings.port", _integer),
    "PACKET_MAP_PASSWORD": EnvSetting("plugins[packet_map].settings.password", secret=True),
    "PACKET_MAP_DB_PATH": EnvSetting("plugins[packet_map].settings.db_path"),
}


@dataclass(slots=True)
class EnvValues:
    """Variables from a ``.env`` file under the process environment, which wins (as with ``load_dotenv``)."""

    file_values: dict[str, str]
    file_name: str
    environ: Mapping[str, str]

    def lookup(self, name: str) -> tuple[str, str] | None:
        """``(value, where it came from)``, or ``None`` when unset or empty."""
        if name in self.environ:
            value, origin = self.environ[name], "environment"
        elif name in self.file_values:
            value, origin = self.file_values[name], self.file_name
        else:
            return None
        if not value.strip():
            return None
        return value, origin


@dataclass(slots=True)
class Change:
    path: str
    source: str
    old: Any
    new: Any
    secret: bool


@dataclass(slots=True)
class Report:
    changes: list[Change] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _canonical_plugin_name(name: Any) -> str:
    return str(name).strip().replace("-", "_")


def _plugin_entries(data: CommentedMap, plugin: str) -> list[CommentedMap]:
    plugins = data.get("plugins")
    if not isinstance(plugins, list):
        return []
    return [
        entry
        for entry in plugins
        if isinstance(entry, dict) and _canonical_plugin_name(entry.get("name", "")) == plugin
    ]


def _active_backend(data: CommentedMap) -> str:
    mesh = data.get("mesh")
    raw = mesh.get("backend", MESHTASTIC_BACKEND) if isinstance(mesh, dict) else MESHTASTIC_BACKEND
    backend = str(raw).strip().lower()
    if backend not in SUPPORTED_BACKENDS:
        raise MigrationError(f"mesh.backend must be one of: {sorted(SUPPORTED_BACKENDS)}; got {raw!r}")
    return backend


def _plain_is_safe(value: str) -> bool:
    """Whether a YAML 1.1 reader (PyYAML) reads ``value`` back unquoted as the same string."""
    try:
        return yaml.safe_load(value) == value
    except yaml.YAMLError:
        return False


def _yaml_value(value: Any, old: Any) -> Any:
    if not isinstance(value, str):
        return value
    if isinstance(old, ScalarString):
        return type(old)(value)  # Keep the quoting style already in the file.
    # Quote what PyYAML would take for something else, like "on", "12:30" or "0x1f".
    return value if _plain_is_safe(value) else DoubleQuotedScalarString(value)


def _same(old: Any, new: Any) -> bool:
    # Keep True apart from 1, which compare equal in Python.
    return old is not MISSING and isinstance(old, bool) == isinstance(new, bool) and old == new


class _Migration:
    """Moves settings from ``env`` into ``data`` (a round-trip YAML mapping), in place."""

    def __init__(self, data: CommentedMap, env: EnvValues) -> None:
        self.data = data
        self.env = env
        self.report = Report()
        self.used: set[str] = set()
        self._inserted: dict[int, int] = {}

    def run(self) -> Report:
        self._apply_env_settings()
        self._inline_dm_http_command()
        self._note_leftovers()
        return self.report

    def _insert(self, mapping: CommentedMap, key: str, value: Any) -> None:
        # New keys go at the top, in the order they're added: a comment after a
        # mapping's last key belongs to that key, so appending would put the new
        # key below the comments that introduce the next section.
        position = self._inserted.get(id(mapping), 0)
        mapping.insert(position, key, value)
        self._inserted[id(mapping)] = position + 1

    def _child_mapping(self, parent: CommentedMap, key: str, path: str) -> CommentedMap:
        value = parent.get(key)
        if isinstance(value, dict):
            return value
        if value is not None:
            raise MigrationError(f"{path} must be a mapping to add settings to it; got {value!r}")
        child = CommentedMap()
        if key in parent:
            parent[key] = child
        else:
            self._insert(parent, key, child)
        return child

    def _targets(self, path: str) -> Iterator[tuple[CommentedMap, str, str]]:
        """Yield ``(mapping, key, display path)`` for each place a setting goes, creating sections as needed."""
        head, *rest = path.replace("{backend}", _active_backend(self.data)).split(".")
        plugin = PLUGIN_SEGMENT.fullmatch(head)
        if plugin is None:
            parents = [(self.data, "")]
            rest = [head, *rest]
        else:
            parents = [
                (entry, f"plugins[{entry.get('name')}].") for entry in _plugin_entries(self.data, plugin.group(1))
            ]

        for mapping, prefix in parents:
            *sections, key = rest
            for depth, section in enumerate(sections, start=1):
                mapping = self._child_mapping(mapping, section, prefix + ".".join(sections[:depth]))
            yield mapping, key, prefix + ".".join(rest)

    def _set(self, mapping: CommentedMap, key: str, value: Any, *, path: str, source: str, secret: bool) -> None:
        old = mapping.get(key, MISSING)
        self.report.changes.append(Change(path, source, old, value, secret))
        if _same(old, value):
            return  # Leave the original formatting alone.
        new = _yaml_value(value, old)
        if key in mapping:
            mapping[key] = new
        else:
            self._insert(mapping, key, new)

    def _apply_env_settings(self) -> None:
        for name, setting in ENV_SETTINGS.items():
            found = self.env.lookup(name)
            if found is None:
                continue
            self.used.add(name)
            raw, origin = found
            try:
                value = setting.convert(raw)
            except ValueError:
                raise MigrationError(
                    f"{name}={raw!r} ({origin}) isn't a valid value; fix or remove it and run again"
                ) from None

            if name == "MESH_BACKEND" and value not in SUPPORTED_BACKENDS:
                raise MigrationError(f"{name} must be one of: {sorted(SUPPORTED_BACKENDS)}; got {raw!r} ({origin})")
            if name == "MESH_MODE":
                backend = _active_backend(self.data)
                if value not in BACKEND_MODES[backend]:
                    raise MigrationError(
                        f"{name} must be one of {sorted(BACKEND_MODES[backend])} for the {backend} backend; "
                        f"got {raw!r} ({origin})"
                    )

            targets = list(self._targets(setting.path))
            if not targets:
                plugin = PLUGIN_SEGMENT.match(setting.path)
                self.report.notes.append(
                    f"{name} ({origin}): the config has no {plugin.group(1) if plugin else setting.path} plugin, "
                    "so it had no effect"
                )
            for mapping, key, path in targets:
                self._set(mapping, key, value, path=path, source=f"{name} ({origin})", secret=setting.secret)

    def _inline_templates(self, mapping: CommentedMap, key: str, path: str, *, secret: bool) -> None:
        sources: list[str] = []
        missing: list[str] = []

        def _replace(match: re.Match[str]) -> str:
            name = match.group(1)
            found = self.env.lookup(name)
            if found is None:
                missing.append(name)
                return match.group(0)
            self.used.add(name)
            sources.append(f"{name} ({found[1]})")
            return found[0]

        inlined = ENV_TEMPLATE_PATTERN.sub(_replace, mapping[key])
        if sources:
            self._set(mapping, key, inlined, path=path, source=", ".join(sources), secret=secret)
        for name in missing:
            self.report.notes.append(
                f"{path}: ${{{name}}} isn't set, so it's left as is; Meshgram still fills it in from the environment"
            )

    def _inline_dm_http_command(self) -> None:
        """Replace ``${VAR}`` and ``auth.token_env`` references with their values, so the secrets live in the config too."""
        for entry in _plugin_entries(self.data, "dm_http_command"):
            settings = entry.get("settings")
            commands = settings.get("commands") if isinstance(settings, dict) else None
            if not isinstance(commands, dict):
                continue
            for command_name, command in commands.items():
                if not isinstance(command, dict):
                    continue
                base = f"plugins[{entry.get('name')}].settings.commands.{command_name}"
                if isinstance(command.get("url"), str):
                    self._inline_templates(command, "url", f"{base}.url", secret=False)
                headers = command.get("headers")
                if isinstance(headers, dict):
                    for header, value in list(headers.items()):
                        if isinstance(value, str):
                            self._inline_templates(headers, header, f"{base}.headers.{header}", secret=True)
                auth = command.get("auth")
                if isinstance(auth, dict) and not auth.get("token"):
                    self._inline_token_env(auth, f"{base}.auth")

    def _inline_token_env(self, auth: CommentedMap, path: str) -> None:
        # The plugin prefers token_env over env.
        key = next((candidate for candidate in ("token_env", "env") if candidate in auth), None)
        if key is None:
            return
        name = str(auth[key]).strip()
        found = self.env.lookup(name)
        if found is None:
            self.report.notes.append(
                f"{path}.{key}: {name} isn't set, so it's left as is; Meshgram still reads it from the environment"
            )
            return
        self.used.add(name)
        # Swap the key in place, keeping any comment attached to it.
        position = list(auth).index(key)
        comment = auth.ca.items.pop(key, None)
        del auth[key]
        auth.insert(position, "token", _yaml_value(found[0], MISSING))
        if comment is not None:
            auth.ca.items["token"] = comment
        self.report.changes.append(Change(f"{path}.token", f"{name} ({found[1]})", MISSING, found[0], True))

    def _note_leftovers(self) -> None:
        """Explain what happens to each ``.env`` entry that wasn't moved."""
        env = self.env
        for name, value in env.file_values.items():
            if name in self.used or not value.strip() or name in ENV_SETTINGS:
                continue
            if name in (CONFIG_PATH_ENV, DATA_DIR_ENV):
                self.report.notes.append(
                    f"{name} ({env.file_name}): still an environment variable (a path, not a setting), but .env "
                    "isn't loaded any more; set it where Meshgram runs if you need a non-default value"
                )
            else:
                self.report.notes.append(
                    f"{name} ({env.file_name}): not used by Meshgram; keep it only if something else "
                    "(like Docker Compose) reads it"
                )
        if "MESH_DEVICE" in self.used and "MESH_DEVICE" in env.file_values:
            self.report.notes.append(
                f"MESH_DEVICE ({env.file_name}): Docker Compose also reads it to pass the radio into the container "
                "(docker-compose.linux-serial.yml); keep that line if you use the overlay with a device other "
                "than /dev/ttyUSB0"
            )


def migrate(data: CommentedMap, env: EnvValues) -> Report:
    """Move the settings ``env`` holds into ``data`` (a round-trip YAML mapping), in place."""
    return _Migration(data, env).run()


def _guess_mapping_indent(text: str) -> int:
    # The first indented key; ruamel's guess only covers sequences.
    for line in text.splitlines():
        stripped = line.lstrip(" ")
        if stripped and len(stripped) < len(line) and not stripped.startswith(("#", "-")):
            return len(line) - len(stripped)
    return 2


def _round_trip_yaml(text: str) -> YAML:
    """A YAML loader/dumper that keeps comments, quoting and the file's indentation."""
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


def _validate(text: str, path: str) -> str | None:
    """Why Meshgram would refuse the migrated config, or ``None`` if it loads."""
    try:
        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            return "the config must be a mapping"
        build_settings(data, config_path=path)
    except (ConfigError, ValueError, yaml.YAMLError) as exc:
        return str(exc)
    return None


def _stale_comment_lines(text: str) -> list[int]:
    return [number for number, line in enumerate(text.splitlines(), start=1) if STALE_COMMENT.search(line)]


def _write_private(path: Path, text: str, *, force: bool) -> None:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if force else os.O_EXCL)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        raise MigrationError(f"{path} already exists; pass --force to overwrite it") from None
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    # The mode passed to open() only applies to a new file.
    os.chmod(path, 0o600)


def _show(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return f'"{value}"'
    return str(value)


def _describe(change: Change) -> str:
    unchanged = _same(change.old, change.new)
    if change.secret:
        return "(hidden, unchanged)" if unchanged else "(hidden)"
    if unchanged:
        return f"{_show(change.new)} (unchanged)"
    if change.old is MISSING:
        return _show(change.new)
    return f"{_show(change.old)} -> {_show(change.new)}"


def _print_report(report: Report, *, stale_lines: Sequence[int], out: TextIO) -> None:
    if report.changes:
        print("\nMoved into the config:", file=out)
        path_width = max(len(change.path) for change in report.changes)
        for change in report.changes:
            print(f"  {change.path:<{path_width}}  {_describe(change)}  <- {change.source}", file=out)
    else:
        print("\nNo settings to move.", file=out)
    if report.notes:
        print("\nNot moved:", file=out)
        for note in report.notes:
            print(f"  {note}", file=out)
    if stale_lines:
        lines = ", ".join(str(number) for number in stale_lines)
        print(f"\nComments that still mention .env or the old variables (lines {lines}): update them by hand.", file=out)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m meshgram.migrate_config",
        description=(
            "Merge the settings from .env (and the environment) into a copy of config.yaml, applying the old "
            "precedence rules, so every setting lives in one file. Comments and layout are kept."
        ),
    )
    parser.add_argument(
        "--config",
        help=f"existing config to start from (default: ${CONFIG_PATH_ENV} if set, else {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE, help="dotenv file to read (default: %(default)s)")
    parser.add_argument(
        "-o", "--output", default=DEFAULT_OUTPUT, help='file to write, or "-" for standard output (default: %(default)s)'
    )
    parser.add_argument("--force", action="store_true", help="overwrite the output file if it exists")
    parser.add_argument(
        "--ignore-environment",
        action="store_true",
        help="read only the dotenv file, not this process's environment variables",
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace, *, environ: Mapping[str, str], out: TextIO | None = None) -> int:
    """Migrate as ``args`` say and print a report to ``out`` (default stderr); returns the exit status."""
    out = out or sys.stderr
    env_file = Path(args.env_file)
    file_values: dict[str, str] = {}
    if env_file.is_file():
        file_values = {name: value for name, value in dotenv_values(env_file).items() if value is not None}
    env = EnvValues(file_values, args.env_file, {} if args.ignore_environment else environ)

    config_found = env.lookup(CONFIG_PATH_ENV)
    config_path = Path(args.config or (config_found[0] if config_found else DEFAULT_CONFIG_PATH))
    if not config_path.is_file():
        raise MigrationError(
            f"{config_path} not found. If `git pull` just removed it (it isn't tracked any more), restore your "
            f"copy with `git show ORIG_HEAD:{DEFAULT_CONFIG_PATH} > {DEFAULT_CONFIG_PATH}` and run again"
        )

    text = config_path.read_text(encoding="utf-8")
    round_trip = _round_trip_yaml(text)
    data = round_trip.load(text)
    if data is None:
        data = CommentedMap()
    if not isinstance(data, CommentedMap):
        raise MigrationError(f"{config_path} must contain a top-level mapping")

    report = migrate(data, env)
    buffer = io.StringIO()
    round_trip.dump(data, buffer)
    output_text = buffer.getvalue()
    problem = _validate(output_text, args.output)

    if args.output == "-":
        sys.stdout.write(output_text)
    else:
        _write_private(Path(args.output), output_text, force=args.force)

    sources = f"{config_path} and {args.env_file}" if file_values else f"{config_path} ({args.env_file} is missing or empty)"
    if not args.ignore_environment:
        sources += " plus the environment"
    print(f"Read {sources}.", file=out)
    _print_report(report, stale_lines=_stale_comment_lines(output_text), out=out)

    if problem is not None:
        print(f"\nMeshgram can't use the result yet: {problem}", file=out)
        return 1
    if args.output != "-":
        # Not a restored copy like config.old.yaml: the file Meshgram reads.
        target = config_path if config_path.name == DEFAULT_CONFIG_PATH else config_path.with_name(DEFAULT_CONFIG_PATH)
        print(
            f"\nWrote {args.output} (readable only by you: it holds secrets). Review it, then replace your config:\n"
            f"  mv {args.output} {target}\n"
            f"Once Meshgram runs with it, delete {args.env_file}.",
            file=out,
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return run(args, environ=os.environ)
    except MigrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
