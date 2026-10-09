# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

**Run tests:**
```bash
.venv/bin/python -m unittest discover -s tests
```

**Run a single test file:**
```bash
.venv/bin/python -m unittest tests.test_bridge_plugin
```

**Run locally:**
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill in TELEGRAM_BOT_TOKEN, TELEGRAM_GROUP_ID, etc.
python main.py
```

**Docker (most common for deployment):**
```bash
docker compose up --build -d                                                          # base
docker compose -f docker-compose.yml -f docker-compose.linux-serial.yml up --build   # serial device
docker compose -f docker-compose.yml -f docker-compose.macos-tcp.yml up --build      # macOS TCP bridge
```

## Architecture

Meshgram is a **plugin-based bidirectional bridge between a mesh radio and Telegram**. It supports two backends, selectable at deploy time:

- **`meshtastic`** (default) — Meshtastic devices over serial or TCP, via the `meshtastic` Python library.
- **`meshcore`** — MeshCore companion radios over serial, TCP, or BLE, via the `meshcore` Python library.

Backend selection: `MESH_BACKEND=meshtastic|meshcore` env var, or `mesh.backend` in `config.yaml`. Existing Meshtastic deployments continue to work with no env/config changes (backend defaults to `meshtastic`).

MeshCore caveats vs. Meshtastic:
- No packet-level reactions — Telegram→MeshCore reaction actions are dropped with a debug log; MeshCore never emits reaction events.
- No reply threading — `SendMeshAction.reply_id` is silently dropped on the meshcore backend (the message still goes out as plain text).
- Identifiers are opaque strings (synthetic IDs derived from `expected_ack` codes and message timestamps) rather than 32-bit numeric packet IDs.

The bridge relays messages, replies (Meshtastic only), and emoji reactions (Meshtastic only) across both platforms.

### Runtime flow

`main.py` → `MeshgramApp.run()` in `meshgram/app.py`:

1. Settings loaded from `.env` + `config.yaml` (env vars override YAML for connection fields)
2. Two transports initialized: `MeshtasticClient` (serial or TCP) and python-telegram-bot `Application`
3. Incoming packets/messages are normalized into typed event dataclasses (`TelegramMessageEvent`, `MeshtasticTextEvent`, `TelegramReactionEvent`, `MeshtasticReactionEvent`) defined in `meshgram/types.py`
4. Each event is dispatched to all enabled plugins (async), collecting `PluginAction` objects in return
5. Actions are executed: send Telegram message, send Meshtastic text (with chunking/ACK/retry), forward reactions

### Plugin system

`meshgram/plugin.py` defines `BasePlugin` with async hooks:
- `on_telegram_message`, `on_meshtastic_message`
- `on_telegram_reaction`, `on_meshtastic_reaction`
- Optional lifecycle hooks: `on_startup`, `on_mesh_connected(transport, context)` (after every radio (re)connect), `on_shutdown`

Each returns a list of `PluginAction` objects (`SendTelegramAction`, `SendMeshtasticAction`, `SendMeshtasticReactionAction`). New plugins are registered in `BUILTIN_PLUGINS` and enabled via `config.yaml`.

**Built-in plugins:**
- `plugins/bridge.py` — core relay with reply linking and reaction sync
- `plugins/ping_pong.py` — keyword-response automation with dedupe and channel filtering
- `plugins/dm_http_command.py` — DMs that invoke HTTP endpoints and return formatted responses
- `plugins/trace_me.py` — MeshCore-only route trace responder
- `plugins/meshmapper.py` — MeshCore-only MeshMapper observer: uploads every RX packet to MeshMapper's MQTT broker (paho-mqtt, device-signed JWT auth). Uses the optional `on_mesh_connected` / `on_shutdown` plugin hooks and `MeshCoreTransport.add_rx_log_listener()` instead of message hooks. Device-signed (publisher) logins can't subscribe on MeshMapper's broker (meshcore-mqtt-broker closes the connection; only operator-issued `SUBSCRIBER_N` accounts can read), so other observers' packets come from `MeshMapperLiveFeed`: MeshMapper's public, account-free "Beacon" WebSocket (`wss://analyzer.meshmapper.net/ws`, the maps' Visualize Live feed; `websockets` library), filtered to the region. Its observations carry header fields/path/observer/signal but no packet bytes, so they're dispatched as `decoded` (+ optional `source_node`) instead of `payload`. With a username/password subscriber account (`MESHMAPPER_SUBSCRIBE_USERNAME`/`_PASSWORD`) a second MQTT session (`MeshMapperSubscriber`) also subscribes to the region's `packets` topics (full packets); the live feed stands by while it's subscribed. Both pass packets to `MeshCoreTransport.dispatch_remote_rx_log()`.
- `plugins/packet_map.py` — MeshCore-only web app (stdlib asyncio HTTP server + Server-Sent Events) showing a Leaflet map of positioned nodes/repeaters with animated packet routes, a live list of every RX packet, and a Messages tab (table of decrypted channel messages, replayable on the map). The page is `plugins/packet_map_static/index.html` (single file, no build step). Node positions come from adverts and `MeshCoreTransport.contacts`. Other observers' packets arrive via `add_remote_rx_log_listener()` and are decrypted with `MeshCoreTransport.channels` (`meshcore_packets.decrypt_group_text`). The header shows connection indicators from `PluginContext.status`. Nodes and packet history persist across restarts in SQLite (`plugins/packet_map_store.py`, under `MESHGRAM_DATA_DIR`, a named Docker volume at `/app/data`); `PacketMapState` tracks changes and the plugin flushes them every few seconds via `asyncio.to_thread`.

### Key modules

| File | Role |
|------|------|
| `meshgram/app.py` | `MeshgramApp`; event dispatch; action execution; Telegram-side helpers |
| `meshgram/transport/__init__.py` | `MeshTransport` ABC + `create_transport()` factory |
| `meshgram/transport/meshtastic.py` | `MeshtasticTransport` (also exported as `MeshtasticClient` for back-compat) |
| `meshgram/transport/meshcore.py` | `MeshCoreTransport` |
| `meshgram/meshcore_packets.py` | Raw MeshCore RF packet decoding (header, path, packet hash, advert contents); shared by `meshmapper` and `packet_map` |
| `meshgram/_mesh_helpers.py` | Shared helpers (node-id normalization, emoji extraction, port-num check) |
| `meshgram/config.py` | Settings dataclasses; `load_settings()` with env-over-YAML precedence |
| `meshgram/types.py` | All event and action dataclasses; `Plugin` protocol; `PluginContext`. Type names use the `Mesh*` prefix; the older `Meshtastic*` names are kept as aliases. |
| `meshgram/status.py` | `StatusRegistry`: thread-safe connection status (radio, telegram, mqtt_publish, mqtt_subscribe, …) set by the app and plugins, shown by `packet_map` |
| `meshgram/reply_links.py` | In-memory bidirectional Telegram↔mesh message ID registry with TTL |
| `meshgram/text_utils.py` | UTF-8 byte-aware chunking for radio MTU constraints |

### Config

- **`.env`** — secrets (bot token, group ID, device path)
- **`config.yaml`** — runtime behavior (bridge channel, node name overrides, Telegram sender template, chunking params, plugin enable/disable + per-plugin settings)
- Env vars `MESH_MODE`, `MESH_HOST`, `MESH_PORT`, `MESH_DEVICE` override YAML connection settings at runtime

### Node name resolution

Sender display names resolve in order: `node_name_overrides` (config.yaml) → `shortName` → `longName` → normalized node ID.

### Message chunking

`text_utils.split_for_meshtastic()` splits messages UTF-8 byte-aware to stay within radio MTU. Chunking config controls max bytes, inter-chunk delay, retry backoff, and ACK wait behavior — all handled transparently by the action executor in `app.py`.
