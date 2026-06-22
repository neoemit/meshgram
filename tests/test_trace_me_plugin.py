import asyncio
import unittest
from typing import Any, cast

from meshgram.config import MESHCORE_BACKEND, MESHTASTIC_BACKEND, MeshgramSettings, PluginConfig
from meshgram.plugin import load_plugins
from meshgram.plugins.trace_me import TraceMePlugin, format_trace_response, split_path_hashes
from meshgram.types import MeshTextEvent, PluginContext, SendMeshAction


class TraceMeFormattingTests(unittest.TestCase):
    def test_split_one_byte_hash_path(self):
        self.assertEqual(split_path_hashes("ff2e02", path_hash_mode=0), ["ff", "2e", "02"])

    def test_split_two_byte_hash_path(self):
        self.assertEqual(split_path_hashes("a1b2c3d4", path_hash_mode=1), ["a1b2", "c3d4"])

    def test_split_three_byte_hash_path(self):
        self.assertEqual(split_path_hashes("a1b2c3ddeeff", path_hash_mode=2), ["a1b2c3", "ddeeff"])

    def test_format_with_path_hashes(self):
        raw_packet = {"path": "ff2e02", "path_len": 3, "path_hash_mode": 0}
        self.assertEqual(format_trace_response(raw_packet), "ff,2e,02 (3 hops)")

    def test_format_zero_hop_without_path(self):
        raw_packet = {"path_len": 0, "path_hash_mode": 0}
        self.assertEqual(format_trace_response(raw_packet), "0 hops")

    def test_format_known_hops_without_path_hashes(self):
        raw_packet = {"path_len": 3, "path_hash_mode": 0}
        self.assertEqual(format_trace_response(raw_packet), "3 hops (repeater list unavailable)")

    def test_direct_sentinel_without_path_does_not_report_255_hops(self):
        raw_packet = {"path_len": 255, "path_hash_mode": -1}
        self.assertEqual(format_trace_response(raw_packet), "0 hops")


def make_context(backend=MESHCORE_BACKEND):
    settings = MeshgramSettings(
        telegram_bot_token="token",
        telegram_group_id=-100,
        config_path="config.yaml",
        plugins=[],
    )
    settings.mesh.backend = backend
    return PluginContext(
        settings=settings,
        telegram_group_id=settings.telegram_group_id,
        mesh_payload_limit=140,
        local_node_id="localnode",
    )


def make_event(**overrides: Any):
    values: dict[str, Any] = dict(
        from_id="cafebabecafe",
        to_id=None,
        packet_id="mc-ch-abc",
        reply_id=None,
        channel_index=2,
        text="Trace",
        sender_label="alice",
        raw_packet={"path": "ff2e02", "path_len": 3, "path_hash_mode": 0},
    )
    values.update(overrides)
    return MeshTextEvent(**values)


def first_mesh_action(actions) -> SendMeshAction:
    return cast(SendMeshAction, actions[0])


class TraceMePluginBehaviorTests(unittest.TestCase):
    def test_meshcore_trace_command_replies_with_path(self):
        plugin = TraceMePlugin({})
        actions = asyncio.run(plugin.on_mesh_message(make_event(), make_context()))
        self.assertEqual(len(actions), 1)
        action = first_mesh_action(actions)
        self.assertEqual(action.text, "ff,2e,02 (3 hops)")
        self.assertEqual(action.channel_index, 2)
        self.assertEqual(action.reply_id, "mc-ch-abc")

    def test_meshcore_trace_command_matches_case_and_edge_punctuation(self):
        plugin = TraceMePlugin({})
        actions = asyncio.run(plugin.on_mesh_message(make_event(text="  ...tRaCe!!! "), make_context()))
        self.assertEqual(len(actions), 1)
        self.assertEqual(first_mesh_action(actions).text, "ff,2e,02 (3 hops)")

    def test_meshtastic_backend_is_ignored(self):
        plugin = TraceMePlugin({})
        actions = asyncio.run(plugin.on_mesh_message(make_event(), make_context(MESHTASTIC_BACKEND)))
        self.assertEqual(actions, [])

    def test_channel_allowlist_blocks_other_channels(self):
        plugin = TraceMePlugin({"channels": [1]})
        actions = asyncio.run(plugin.on_mesh_message(make_event(channel_index=2), make_context()))
        self.assertEqual(actions, [])

    def test_channel_allowlist_allows_string_csv_channels(self):
        plugin = TraceMePlugin({"channels": "1, 2"})
        actions = asyncio.run(plugin.on_mesh_message(make_event(channel_index=2), make_context()))
        self.assertEqual(len(actions), 1)

    def test_response_channel_override(self):
        plugin = TraceMePlugin({"response_channel": 0})
        actions = asyncio.run(plugin.on_mesh_message(make_event(channel_index=2), make_context()))
        self.assertEqual(first_mesh_action(actions).channel_index, 0)

    def test_non_exact_keyword_is_ignored(self):
        plugin = TraceMePlugin({})
        actions = asyncio.run(plugin.on_mesh_message(make_event(text="trace me"), make_context()))
        self.assertEqual(actions, [])

    def test_custom_keyword_can_be_configured(self):
        plugin = TraceMePlugin({"keywords": ["route"]})
        default_actions = asyncio.run(plugin.on_mesh_message(make_event(text="trace"), make_context()))
        custom_actions = asyncio.run(plugin.on_mesh_message(make_event(text="Route"), make_context()))
        self.assertEqual(default_actions, [])
        self.assertEqual(len(custom_actions), 1)

    def test_dm_like_meshcore_trace_message_is_ignored(self):
        plugin = TraceMePlugin({})
        event = make_event(channel_index=-1, to_id="localnode")
        actions = asyncio.run(plugin.on_mesh_message(event, make_context()))
        self.assertEqual(actions, [])

    def test_trace_me_builtin_plugin_loads(self):
        loaded = load_plugins([PluginConfig(name="trace_me", enabled=True, settings={})])
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].name, "trace_me")
        self.assertEqual(loaded[0].instance.name, "trace_me")

    def test_trace_me_hyphenated_alias_plugin_loads(self):
        loaded = load_plugins([PluginConfig(name="trace-me", enabled=True, settings={})])
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].name, "trace-me")
        self.assertEqual(loaded[0].instance.name, "trace_me")


if __name__ == "__main__":
    unittest.main()
