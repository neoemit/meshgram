"""config.yaml with the control panel's plugin changes in it.

The control panel never writes config.yaml: it holds secrets and is usually
mounted read-only. Plugin changes made there are saved in
``$MESHGRAM_DATA_DIR/plugins.json`` and win over config.yaml. To make them the
new defaults, the panel offers config.yaml as it would read with those changes
in it: the file on disk, comments, quoting and layout kept, with each changed
plugin's ``enabled`` and ``settings`` replaced. Once that file replaces
config.yaml and Meshgram restarts, the saved changes match config.yaml and are
dropped (see ``PluginManager``).

The result is checked by loading it the way Meshgram does: every plugin must
come out as the control panel has it, and nothing else may change.
"""

from __future__ import annotations

import dataclasses
import difflib
import io
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

import yaml
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import CommentMark, YAMLError as RoundTripYAMLError
from ruamel.yaml.tokens import CommentToken

from .config import DEFAULT_CONFIG_PATH, ConfigError, MeshgramSettings, PluginConfig, build_settings, canonical_plugin_name
from .yaml_round_trip import MISSING, round_trip_yaml, same, yaml_value


class ConfigExportError(Exception):
    """config.yaml, as it is on disk, can't be exported."""


@dataclass(slots=True)
class PluginChange:
    """How a plugin differs between config.yaml and the export."""

    name: str
    enabled: Optional[bool] = None  # The new on/off state, when it changes.
    settings: bool = False  # Whether its settings change.


@dataclass(slots=True)
class ConfigExport:
    text: str
    # Unified diff from config.yaml to ``text``; empty when they're the same.
    diff: str = ""
    changes: list[PluginChange] = field(default_factory=list)
    # Where the result doesn't load back as intended; worth a look before using it.
    problems: list[str] = field(default_factory=list)


def export_config(
    text: str,
    overrides: Mapping[str, Mapping[str, Any]],
    *,
    path: str = DEFAULT_CONFIG_PATH,
) -> ConfigExport:
    """``text`` (config.yaml) with ``overrides`` (plugin name -> ``enabled`` and/or ``settings``) in it."""
    original = _load(text, path)
    if not overrides:
        return ConfigExport(text)

    round_trip = round_trip_yaml(text)
    try:
        data = round_trip.load(text)
    except RoundTripYAMLError as exc:
        raise ConfigExportError(f"{path} can't be edited: {exc}") from None
    if not isinstance(data, CommentedMap):
        raise ConfigExportError(f"{path} must contain a top-level mapping")

    entries = _plugin_list(data, original.plugins)
    for name, override in overrides.items():
        _apply(entries, canonical_plugin_name(name), override)
    buffer = io.StringIO()
    round_trip.dump(data, buffer)
    exported_text = buffer.getvalue()

    try:
        exported = _load(exported_text, path)
    except ConfigExportError as exc:
        return ConfigExport(exported_text, _diff(text, exported_text, path), problems=[str(exc)])
    changes, problems = _compare(original, exported, overrides)
    if not changes and not problems:
        # config.yaml already says it all; don't offer the round trip's reformatting.
        return ConfigExport(text)
    return ConfigExport(exported_text, _diff(text, exported_text, path), changes, problems)


# --- Loading -----------------------------------------------------------------------------


def _load(text: str, path: str) -> MeshgramSettings:
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigExportError(f"{path} isn't valid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise ConfigExportError(f"{path} must contain a top-level mapping")
    try:
        return build_settings(data, config_path=path)
    except (ConfigError, ValueError) as exc:
        raise ConfigExportError(f"Meshgram can't use {path}: {exc}") from None


def _plugins_by_name(settings: MeshgramSettings) -> dict[str, PluginConfig]:
    plugins: dict[str, PluginConfig] = {}
    for plugin in settings.plugins:
        plugins.setdefault(canonical_plugin_name(plugin.name), plugin)  # The first entry wins.
    return plugins


# --- Editing -----------------------------------------------------------------------------


def _entry_name(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    return canonical_plugin_name(item.get("name") or "")


def _plugin_list(data: CommentedMap, effective: list[PluginConfig]) -> CommentedSeq:
    """The ``plugins`` list to edit; spelled out first when Meshgram runs its defaults instead."""
    plugins = data.get("plugins")
    if isinstance(plugins, CommentedSeq) and any(_entry_name(item) for item in plugins):
        return plugins
    # No usable list, so Meshgram runs the default plugins. Write them out, or
    # adding the changed plugins would take their place.
    if not isinstance(plugins, CommentedSeq):
        plugins = CommentedSeq()
        if "plugins" not in data and data:
            _blank_line_before(data, "plugins")
        data["plugins"] = plugins
    for plugin in effective:
        entry = CommentedMap()
        entry["name"] = plugin.name
        entry["enabled"] = plugin.enabled
        if plugin.settings:
            entry["settings"] = _to_yaml(plugin.settings)
        _append_entry(plugins, entry)
    return plugins


def _apply(entries: CommentedSeq, name: str, override: Mapping[str, Any]) -> None:
    entry = next((item for item in entries if _entry_name(item) == name), None)
    if entry is None:
        entry = CommentedMap()
        entry["name"] = name
        # A plugin config.yaml doesn't list is off; a listed one without "enabled" is on.
        entry["enabled"] = bool(override.get("enabled", False))
        if override.get("settings"):
            entry["settings"] = _to_yaml(override["settings"])
        _append_entry(entries, entry)
        return

    if "enabled" in override:
        enabled = bool(override["enabled"])
        if "enabled" in entry:
            _merge_into(entry, "enabled", enabled)
        elif not enabled:
            _insert(entry, list(entry).index("name") + 1 if "name" in entry else 0, "enabled", False)
    if "settings" in override:
        settings = override["settings"]
        old = entry.get("settings")
        if isinstance(old, CommentedMap) and isinstance(settings, dict):
            _merge_mapping(old, settings)
        elif "settings" in entry:
            _merge_into(entry, "settings", settings)
        elif settings:
            _insert(entry, len(entry), "settings", _to_yaml(settings))


def _merge_mapping(node: CommentedMap, new: Mapping[Any, Any]) -> None:
    """Make ``node`` equal ``new`` in place, so what stays keeps its comments and style."""
    for key in [key for key in node if key not in new]:
        _delete(node, key)
    previous = MISSING
    for key, value in new.items():
        if key in node:
            _merge_into(node, key, value)
        else:
            # Next to the key before it in ``new``, so related settings stay together.
            position = list(node).index(previous) + 1 if previous is not MISSING else 0
            _insert(node, position, yaml_value(key), _to_yaml(value))
        previous = key


def _merge_sequence(node: CommentedSeq, new: list[Any]) -> None:
    for index, value in enumerate(new):
        if index < len(node):
            _merge_into(node, index, value)
        else:
            node.append(_to_yaml(value))
    while len(node) > len(new):
        del node[len(node) - 1]


def _merge_into(node: Any, key: Any, value: Any) -> None:
    old = node[key]
    if isinstance(old, CommentedMap) and isinstance(value, dict):
        _merge_mapping(old, value)
    elif isinstance(old, CommentedSeq) and isinstance(value, list):
        _merge_sequence(old, value)
    elif not same(old, value):
        following = _detach_following(node, key) if isinstance(node, CommentedMap) else ""
        node[key] = _to_yaml(value, old)
        if following:
            _attach_following(node, key, following)


# Comment lines after a key introduce whatever comes next, but ruamel keeps
# them on that key (on the last scalar under it, for a nested mapping). Keys
# that go away or get a new neighbour hand them on, so they stay in place.


def _insert(node: CommentedMap, position: int, key: Any, value: Any) -> None:
    previous = list(node)[position - 1] if position > 0 else MISSING
    node.insert(position, key, value)
    if previous is not MISSING and _tail(node, key) is not None:
        _attach_following(node, key, _detach_following(node, previous))


def _delete(node: CommentedMap, key: Any) -> None:
    keys = list(node)
    following = _detach_following(node, key)
    del node[key]
    node.ca.items.pop(key, None)
    position = keys.index(key)
    if following and position > 0:
        _attach_following(node, keys[position - 1], following)


def _tail(node: CommentedMap, key: Any) -> Optional[tuple[CommentedMap, Any]]:
    """Where the comment lines after ``node[key]`` (and all it holds) are kept, if they can move."""
    value = node[key]
    while isinstance(value, CommentedMap) and value:
        node, key = value, list(value)[-1]
        value = node[key]
    if isinstance(value, CommentedSeq) and value.fa.flow_style() is not True:
        return None  # Comments after a block sequence sit on its items; leave them be.
    return node, key


def _detach_following(node: CommentedMap, key: Any) -> str:
    """Take the comment lines after ``key``'s line (its own end-of-line comment stays)."""
    tail = _tail(node, key)
    if tail is None:
        return ""
    holder, holder_key = tail
    slot = holder.ca.items.get(holder_key)
    token = slot[2] if slot and len(slot) > 2 else None
    if token is None:
        return ""
    end_of_line, newline, following = token.value.partition("\n")
    if not following:
        return ""
    if end_of_line:
        token.value = end_of_line + newline
    else:
        slot[2] = None
    return following


def _attach_following(node: CommentedMap, key: Any, following: str) -> None:
    tail = _tail(node, key)
    if not following or tail is None:
        return
    holder, holder_key = tail
    slot = holder.ca.items.setdefault(holder_key, [None, None, None, None])
    if slot[2] is None:
        slot[2] = CommentToken("\n" + following, CommentMark(0), None)
    else:
        slot[2].value += following


def _blank_line_before(node: CommentedMap, key: Any) -> None:
    node.ca.items.setdefault(key, [None, None, None, None])[1] = [CommentToken("\n", CommentMark(0), None)]


def _append_entry(entries: CommentedSeq, entry: CommentedMap) -> None:
    """Add a plugin to the list, a blank line apart like the ones before it."""
    if entries.ca.end and isinstance(entries[-1], CommentedMap) and entries[-1]:
        # Comments after the last plugin are kept at the end of the list; they
        # belong to that plugin, so they go with it rather than after the new one.
        last = entries[-1]
        following = "".join(" " * (token.column or 0) + token.value.lstrip(" ") for token in entries.ca.end)
        if _tail(last, list(last)[-1]) is not None:
            _attach_following(last, list(last)[-1], following)
            entries.ca.end = []
    entries.append(entry)
    if len(entries) < 2 or not isinstance(entries[-2], CommentedMap) or not entries[-2]:
        return
    tail = _tail(entries[-2], list(entries[-2])[-1])
    slot = tail[0].ca.items.get(tail[1]) if tail else None
    if slot and slot[2] is not None and slot[2].value.endswith("\n\n"):
        return  # Already a blank line after the one before.
    entries.ca.items.setdefault(len(entries) - 1, [None, None, None, None])[1] = [CommentToken("\n", CommentMark(0), None)]


def _to_yaml(value: Any, old: Any = MISSING) -> Any:
    """``value`` (from JSON) as round-trip YAML that PyYAML reads back unchanged."""
    if isinstance(value, dict):
        mapping = CommentedMap()
        for key, item in value.items():
            mapping[yaml_value(key)] = _to_yaml(item)
        return mapping
    if isinstance(value, list):
        sequence = CommentedSeq(_to_yaml(item) for item in value)
        if not any(isinstance(item, (dict, list)) for item in value):
            sequence.fa.set_flow_style()  # channels: [0, 1]
        return sequence
    return yaml_value(value, old)


# --- Checking ----------------------------------------------------------------------------


def _equal(a: Any, b: Any) -> bool:
    """``==``, except True isn't 1."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_equal(a[key], b[key]) for key in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_equal(x, y) for x, y in zip(a, b))
    return a == b


def _compare(
    original: MeshgramSettings,
    exported: MeshgramSettings,
    overrides: Mapping[str, Mapping[str, Any]],
) -> tuple[list[PluginChange], list[str]]:
    problems: list[str] = []
    if dataclasses.replace(original, plugins=[]) != dataclasses.replace(exported, plugins=[]):
        problems.append("Settings outside the plugins section would change too")

    before = _plugins_by_name(original)
    after = _plugins_by_name(exported)
    wanted = {canonical_plugin_name(name): override for name, override in overrides.items()}
    changes: list[PluginChange] = []
    for name in dict.fromkeys([*before, *after, *wanted]):
        old = before.get(name) or PluginConfig(name, enabled=False)
        new = after.get(name) or PluginConfig(name, enabled=False)
        override = wanted.get(name, {})
        enabled = bool(override.get("enabled", old.enabled))
        settings = override.get("settings", old.settings)
        if new.enabled != enabled or not _equal(new.settings, settings):
            problems.append(f"Plugin {name} doesn't come out as set in the control panel")
        change = PluginChange(
            name,
            enabled=new.enabled if new.enabled != old.enabled else None,
            settings=not _equal(new.settings, old.settings),
        )
        if change.enabled is not None or change.settings:
            changes.append(change)
    return changes, problems


def _diff(old: str, new: str, path: str) -> str:
    if old == new:
        return ""
    lines = difflib.unified_diff(old.splitlines(), new.splitlines(), fromfile=path, tofile=path, lineterm="")
    return "\n".join(lines) + "\n"
