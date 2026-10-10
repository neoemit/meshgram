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
cp config.example.yaml config.yaml  # fill in telegram.bot_token, telegram.group_id, etc.
python main.py
```

**Docker (most common for deployment):**
```bash
docker compose up --build -d                                                          # base
docker compose -f docker-compose.yml -f docker-compose.linux-serial.yml up --build   # serial device
```

## Architecture

Meshgram is a **plugin-based bidirectional bridge between a MeshCore companion radio and Telegram**, with a web app (live packet map, messages, control panel). It talks to the radio over serial, TCP, or BLE via the `meshcore` Python library. Meshtastic support was removed: `mesh.backend` other than `meshcore`, or a config with only a `meshtastic` section, is refused at startup (`config.MESHTASTIC_REMOVED_HINT`).

MeshCore has no packet-level reactions and no reply threading, so the bridge relays messages only. Identifiers are opaque strings (`MeshPacketRef = str`): synthetic IDs derived from `expected_ack` codes and message timestamps.

### Runtime flow

`main.py` → `MeshgramApp.run()` in `meshgram/app.py`:

1. Settings loaded from `config.yaml` only (`load_settings()`); config errors exit with a message, and old setting env vars still set are logged as ignored
2. `MeshCoreTransport` (radio), python-telegram-bot `Application`, `WebServer` (if `web.enabled`) and `PluginManager` are created; the control panel API is registered on the web server (`web.api.register_control_api`)
3. On start: web server, then plugins (`PluginManager.start_all`), then the radio connect loop (`on_mesh_connected` after every (re)connect)
4. Incoming messages are normalized into `TelegramMessageEvent` / `MeshTextEvent` (`meshgram/types.py`) and dispatched to the running plugins (`PluginManager.running`), collecting `PluginAction`s
5. Actions are executed (`MeshgramApp.execute_actions`): send Telegram message, send mesh text (chunk sequences, retry/backoff, abort-on-failure)

### Plugin system

`meshgram/plugin.py` defines `BasePlugin` with async hooks `on_telegram_message`, `on_mesh_message`, and lifecycle hooks `on_startup`, `on_mesh_connected(transport, context)` (after every radio (re)connect, and on start when already connected), `on_shutdown`. Plugins also declare `title`, `description` and `settings_schema` (JSON Schema subset, see `settings_schema.py`; `format: "channel"` marks a channel-slot integer) for the control panel.

Each hook returns `PluginAction`s (`SendTelegramAction`, `SendMeshAction`). Built-ins are registered in `BUILTIN_PLUGINS` (canonical names; "trace-me" style aliases resolve via `plugin_key`) and enabled via `config.yaml`.

`meshgram/plugin_manager.py` runs them and changes them at runtime: `update(name, enabled=…, settings=…)` validates (secrets come back masked as `SECRET_MASK` and are restored), saves the override to `$MESHGRAM_DATA_DIR/plugins.json` (atomic, 0600), then restarts the plugin (`on_shutdown` + new instance). Overrides win over config.yaml until `reset(name)`. Because plugins can now be stopped at runtime, `on_shutdown` must release everything, including transport listeners (`remove_rx_log_listener`) and status entries (`StatusRegistry.remove`).

**Built-in plugins:**
- `plugins/bridge.py` — core relay (Telegram group ↔ one radio channel) with chunking
- `plugins/ping_pong.py` — keyword-response automation with dedupe and channel filtering
- `plugins/dm_http_command.py` — DMs that invoke HTTP endpoints and return formatted responses
- `plugins/trace_me.py` — route trace responder
- `plugins/meshmapper.py` — MeshMapper observer: uploads every RX packet to MeshMapper's MQTT broker (paho-mqtt, device-signed JWT auth). Uses `on_mesh_connected` / `on_shutdown` and `MeshCoreTransport.add_rx_log_listener()` instead of message hooks. Device-signed (publisher) logins can't subscribe on MeshMapper's broker (meshcore-mqtt-broker closes the connection; only operator-issued `SUBSCRIBER_N` accounts can read), so other observers' packets come from `MeshMapperLiveFeed`: MeshMapper's public, account-free "Beacon" WebSocket (`wss://analyzer.meshmapper.net/ws`, the maps' Visualize Live feed; `websockets` library), filtered to the region. Its observations carry header fields/path/observer/signal but no packet bytes, so they're dispatched as `decoded` (+ optional `source_node`) instead of `payload`. With a username/password subscriber account (`subscribe_username`/`subscribe_password` settings) a second MQTT session (`MeshMapperSubscriber`) also subscribes to the region's `packets` topics (full packets); the live feed stands by while it's subscribed. Both pass packets to `MeshCoreTransport.dispatch_remote_rx_log()`.
- `plugins/packet_map.py` — feeds the web app's Map and Messages views: positioned nodes/repeaters (adverts + `MeshCoreTransport.contacts`), every RX packet with resolved routes, decrypted channel messages. It registers a `map` snapshot provider on `context.web.events` and publishes `packet`/`nodes`/`self` events; it calls `events.resync()` on start/stop so open pages notice. Other observers' packets arrive via `add_remote_rx_log_listener()` and are decrypted with `MeshCoreTransport.channels` (`meshcore_packets.decrypt_group_text`). Nodes and packet history persist in SQLite (`plugins/packet_map_store.py`, under `MESHGRAM_DATA_DIR`, a named Docker volume at `/app/data`); `PacketMapState` tracks changes and the plugin flushes them every few seconds via `asyncio.to_thread`.

### Web app

- `web/server.py` — `WebServer`: stdlib asyncio HTTP/1.1 server (no framework), route table with `{param}` patterns, JSON bodies (256 KiB max), HTTP Basic auth (`web.password`), static files from `web/static/` (`index.html` with the injected `/*__MESHGRAM_CONFIG__*/` client config, `control.js`), security headers (CSP, `X-Frame-Options`, `nosniff`; Referrer-Policy must keep sending a Referer: OSM tiles require it). `EventHub`: SSE at `/api/events`, a snapshot merged from providers (core: `connections`, `control`; packet_map: `map`) then live events. Mutating requests (POST/PUT/PATCH/DELETE) need same-origin (`Origin`/`Sec-Fetch-Site`) and `application/json` (CSRF guard), and are refused with 403 unless `WebConfig.allows_changes` (a password is set, or the host is loopback).
- `web/api.py` — the control panel's JSON API (endpoints listed in its docstring): radio overview/settings/actions, channels (add by name), contacts, plugins. Maps domain errors to HTTP (`RadioAdminError` 400/404/409, `RadioCommandError` 502/503, `SettingsError` 422 with `details`). Successful changes publish `{"type": "control", "what": …}` so open pages refresh.
- `web/static/index.html` (map, messages, page shell) + `web/static/control.js` (control panel; uses the page script's global helpers and state). No build step.

### Key modules

| File | Role |
|------|------|
| `meshgram/app.py` | `MeshgramApp`; plugin host (`plugin_context`, `execute_actions`, `connected_transport`); event dispatch; action execution |
| `meshgram/transport/meshcore.py` | `MeshCoreTransport`: connect, inbound events, sends (one command at a time via `_command_lock`), RX log listeners, `command(name, …)` for the control panel (`RadioCommandError`), `channel_slots`, `max_channels`, `refresh_*` |
| `meshgram/radio_admin.py` | `RadioAdmin`: radio overview, validated settings changes, adverts/clock/reboot, channels by name (hashtag key = SHA-256(name)[:16], Public's well-known key, private random/given key), contacts |
| `meshgram/plugin_manager.py` | `PluginManager` (runtime on/off + settings, overrides in `plugins.json`), `PluginOverrideStore` |
| `meshgram/settings_schema.py` | JSON Schema subset validator; `mask_secrets` / `restore_secrets` for `writeOnly` fields |
| `meshgram/meshcore_packets.py` | Raw MeshCore RF packet decoding (header, path, packet hash, advert contents); shared by `meshmapper` and `packet_map` |
| `meshgram/config.py` | Settings dataclasses (`MeshCoreConfig`, `WebConfig`, …); `load_settings()` reads `config.yaml` (path from `MESHGRAM_CONFIG_PATH`), `build_settings()` validates parsed data; `data_dir()`; `LEGACY_ENV_VARS` |
| `meshgram/migrate_config.py` | `python -m meshgram.migrate_config`: one-off merge of old `.env`/env settings into a copy of `config.yaml` (ruamel.yaml round-trip keeps comments); `ENV_SETTINGS` maps each legacy variable to its YAML path |
| `meshgram/types.py` | Event and action dataclasses; `Plugin` protocol; `PluginContext` (`status`, `web`) |
| `meshgram/status.py` | `StatusRegistry`: thread-safe connection status (radio, telegram, mqtt_publish, mqtt_subscribe, …) set by the app and plugins, shown by the web app; `remove(key)` |
| `meshgram/text_utils.py` | UTF-8 byte-aware chunking for radio MTU constraints (`split_for_mesh`) |

### Config

- **`config.yaml`** — the only source of settings, secrets included (Telegram credentials, `meshcore` connection + bridge channel, name overrides, chunking params, `web`, plugins and their settings). Gitignored, kept out of the Docker image (`.dockerignore`) and bind-mounted read-only by `docker-compose.yml`. Never written by Meshgram.
- **`$MESHGRAM_DATA_DIR/plugins.json`** — plugin on/off state and settings changed in the control panel; wins over config.yaml for those plugins until reset there.
- **`config.example.yaml`** — tracked template documenting every field; keep it in sync when adding settings (and the plugin's `settings_schema`).
- `web` falls back to the old `packet_map` plugin settings (`host`, `port`, `password`, `title`, `tile_url`, `tile_attribution`) while config.yaml has no `web` section.
- No env var overrides any setting. The only env vars are paths: `MESHGRAM_CONFIG_PATH` (config file) and `MESHGRAM_DATA_DIR` (persistent state). `dm_http_command` can still reference env vars explicitly (`${VAR}`, `auth.token_env`).
- `.env` is no longer loaded; `python -m meshgram.migrate_config` moves old `.env` settings into the YAML (writes `config.migrated.yaml`).

### Node name resolution

Channel messages carry the sender name in the text (`"<name>: <text>"`, split off by the transport). DMs resolve `contact_name_overrides` (config.yaml) → contact `adv_name` → pubkey prefix.

### Message chunking

`text_utils.split_for_mesh()` splits messages UTF-8 byte-aware to stay within radio MTU. Chunking config controls max bytes (channel messages are broadcasts: `broadcast_max_chunk_bytes`), inter-chunk delay, and retry backoff — all handled by the bridge and the action executor in `app.py`.
