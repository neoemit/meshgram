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

1. Settings loaded from `config.yaml` plus the control panel's saved changes (`CoreSettings` in `meshgram/core_settings.py`, from `$MESHGRAM_DATA_DIR/settings.json`); config errors exit with a message, and old setting env vars still set are logged as ignored
2. `MeshCoreTransport` (radio), python-telegram-bot `Application`, `WebServer` (if `web.enabled`) and `PluginManager` are created; the control panel API is registered on the web server (`web.api.register_control_api`)
3. On start: web server, then plugins (`PluginManager.start_all`), then the radio connect loop (`on_mesh_connected` after every (re)connect)
4. Incoming messages are normalized into `TelegramMessageEvent` / `MeshTextEvent` (`meshgram/types.py`) and dispatched to the running plugins (`PluginManager.running`), collecting `PluginAction`s
5. Actions are executed (`MeshgramApp.execute_actions`): send Telegram message, send mesh text (chunk sequences, retry/backoff, abort-on-failure)
6. A restart from the control panel (`MeshgramApp.request_restart`) stops the polling loop; `main()` then re-executes the same command (`os.execv`), so settings are read anew

### Plugin system

`meshgram/plugin.py` defines `BasePlugin` with async hooks `on_telegram_message`, `on_mesh_message`, and lifecycle hooks `on_startup`, `on_mesh_connected(transport, context)` (after every radio (re)connect, and on start when already connected), `on_shutdown`. Plugins also declare `title`, `description` and `settings_schema` (JSON Schema subset, see `settings_schema.py`; `format: "channel"` marks a channel-slot integer) for the control panel.

Each hook returns `PluginAction`s (`SendTelegramAction`, `SendMeshAction`); outside a hook (an RX log listener, say) a plugin runs them with `context.execute_actions(actions, plugin_name)`. Built-ins are registered in `BUILTIN_PLUGINS` (canonical names; "trace-me" style aliases resolve via `plugin_key`) and enabled via `config.yaml`.

`meshgram/plugin_manager.py` runs them and changes them at runtime: `update(name, enabled=…, settings=…)` validates (secrets come back masked as `SECRET_MASK` and are restored), saves the override to `$MESHGRAM_DATA_DIR/plugins.json` (atomic, 0600), then applies it: if the effective settings changed, the running instance's `apply_settings(settings)` may take them live (return True), otherwise the plugin restarts (`on_shutdown` + new instance). `update`/`reset` report `restarted`. Overrides win over config.yaml until `reset(name)`; `overrides()` returns them unmasked for the config export, and on startup any part of an override that config.yaml now says too is dropped (and `plugins.json` rewritten). Because plugins can now be stopped at runtime, `on_shutdown` must release everything, including transport listeners (`remove_rx_log_listener`) and status entries (`StatusRegistry.remove`).

**Built-in plugins:**
- `plugins/bridge.py` — core relay (Telegram group ↔ one radio channel) with chunking
- `plugins/ping_pong.py` — keyword-response automation with dedupe and channel filtering
- `plugins/dm_http_command.py` — DMs that invoke HTTP endpoints and return formatted responses
- `plugins/trace_me.py` — route trace responder
- `plugins/meshmapper.py` — MeshMapper observer: uploads every RX packet to MeshMapper's MQTT broker (paho-mqtt, device-signed JWT auth). Uses `on_mesh_connected` / `on_shutdown` and `MeshCoreTransport.add_rx_log_listener()` instead of message hooks. Device-signed (publisher) logins can't subscribe on MeshMapper's broker (meshcore-mqtt-broker closes the connection; only operator-issued `SUBSCRIBER_N` accounts can read), so other observers' packets come from `MeshMapperLiveFeed`: MeshMapper's public, account-free "Beacon" WebSocket (`wss://analyzer.meshmapper.net/ws`, the maps' Visualize Live feed; `websockets` library), filtered to the region. Its observations carry header fields/path/observer/signal but no packet bytes, so they're dispatched as `decoded` (+ optional `source_node`) instead of `payload`. With a username/password subscriber account (`subscribe_username`/`subscribe_password` settings) a second MQTT session (`MeshMapperSubscriber`) also subscribes to the region's `packets` topics (full packets); the live feed stands by while it's subscribed. Both pass packets to `MeshCoreTransport.dispatch_remote_rx_log()`.
- `plugins/packet_map.py` — feeds the web app's Map and Messages views: positioned nodes/repeaters (adverts + `MeshCoreTransport.contacts`), every RX packet with resolved routes, decrypted channel messages. It registers a `map` snapshot provider on `context.web.events` (packets plus `hidden_types`) and publishes `packet`/`nodes`/`self` events; `hidden_packet_types` (display only: the page leaves those types out of the list, its filters, counts and flows) changes live via `apply_settings` and a `map_settings` event, anything else restarts it; it calls `events.resync()` on start/stop so open pages notice. Other observers' packets arrive via `add_remote_rx_log_listener()` and are decrypted with `MeshCoreTransport.channels` (`meshcore_packets.decrypt_group_text`). Nodes and packet history persist in SQLite (`plugins/packet_map_store.py`, under `MESHGRAM_DATA_DIR`, a named Docker volume at `/app/data`); `PacketMapState` tracks changes and the plugin flushes them every few seconds via `asyncio.to_thread`.
- `plugins/check_in.py` — weekly check-ins ("#MeshCoreMonday"): a channel message containing the keyword on a check-in day (`days`, `timezone`) counts, from `on_mesh_message` or observers' packets (`add_remote_rx_log_listener`, decrypted with the channel's key from `channel_slots`; deduped across sources by a key of channel hash + sender timestamp + text); the reply (rank this week, streak, participants) goes out via the return value or `context.execute_actions`. Senders are resolved name → key through a node directory fed by adverts and contacts; unknown/ambiguous names are "name only" participants, linked (merged) once the name's advert is heard. `check_in_store.py` (SQLite, the single source of truth), `check_in_scoring.py` (pure week/window/streak/rank math), `check_in_api.py` (routes under `/api/plugins/check_in/`, added on start and removed on stop: the public, token-protected `leaderboard` plus the control panel's management/import/export). The control panel part is `web/static/check_in.js`. The separately deployed leaderboard website is `leaderboard/` (nginx proxy + static page, own docker compose).

### Web app

- `web/server.py` — `WebServer`: stdlib asyncio HTTP/1.1 server (no framework), route table with `{param}` patterns (`route()` returns the route for `remove_route()`; `public=True` routes skip the Basic auth and check access themselves, e.g. with `is_authorized`), JSON bodies (256 KiB max), HTTP Basic auth (`web.password`), static files from `web/static/` (`index.html` with the injected `/*__MESHGRAM_CONFIG__*/` client config, `control.js`, `check_in.js`), security headers (CSP, `X-Frame-Options`, `nosniff`; Referrer-Policy must keep sending a Referer: OSM tiles require it). `EventHub`: SSE at `/api/events`, a snapshot merged from providers (core: `connections`, `control`; packet_map: `map`) then live events. Mutating requests (POST/PUT/PATCH/DELETE) need same-origin (`Origin`/`Sec-Fetch-Site`) and `application/json` (CSRF guard), and are refused with 403 unless `WebConfig.allows_changes` (a password is set, or the host is loopback). `check_secret_access(request)` applies the same to reads that hand out secrets (the config export), plus a loopback `Host` header when there's no password (DNS rebinding).
- `web/api.py` — the control panel's JSON API (endpoints listed in its docstring): radio overview/settings/actions, channels (add by name), contacts, plugins, Meshgram's own settings (`/api/settings`, by section) and restart, and config.yaml with the settings and plugin overrides written in (`GET /api/config` JSON with diff and changed plugins; `GET /api/config.yaml` download; read from disk on every request). Maps domain errors to HTTP (`RadioAdminError` 400/404/409, `RadioCommandError` 502/503, `SettingsError` 422 with `details`). Successful changes publish `{"type": "control", "what": …}` so open pages refresh.
- `web/static/index.html` (map, messages, page shell) + `web/static/control.js` (control panel; uses the page script's global helpers and state). No build step. The settings form is built from each plugin's schema: arrays of `format: "channel"` items and arrays with `items.enum` render as tick boxes (`x-inverted`: the setting lists the unticked values; an array `default` is ticked when unset; `x-keep-empty`: nothing ticked saves `[]` instead of unsetting it), `x-enum-titles` label enum values. `#control/plugins/<name>` opens a plugin's settings. A plugin can add its own panel to its card: a static script calls `MeshgramControl.registerPluginPanel(name, {title, render(container, kit)})` (`kit`: the control panel's helpers); `{"type":"control","what":<name>}` events call the panel's `refresh()`. `x-multiline` strings render as a textarea. The Settings tab renders `core_settings.SCHEMA` the same way (a card per config.yaml section) and offers Restart now when saved settings aren't in use yet. The Config file tab (`#control/config`) shows the export with Copy / Download.

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
| `meshgram/core_settings.py` | `CoreSettings`: everything in config.yaml but the plugins, editable from the control panel. `SCHEMA` (one object per top-level section; secrets `writeOnly`), `core_values(settings)` (values as Meshgram reads them), overrides as dotted paths in `$MESHGRAM_DATA_DIR/settings.json` (only what differs from config.yaml; caught-up ones dropped on startup), `update`/`reset` per section (take effect on restart; `pending()`), `check_telegram` (asks Telegram about a new token/group). Keep `SCHEMA` and `core_values` in sync with `config.py` when adding settings |
| `meshgram/storage.py` | `write_private_json`: atomic, 0600 JSON files in the data dir |
| `meshgram/config_export.py` | `export_config(text, overrides, settings=…)`: config.yaml with plugin overrides and core settings overrides merged into the round-trip YAML (only changed keys touched, comments moved along), then loaded back with PyYAML/`build_settings` to check every plugin comes out as intended and nothing else changed (`problems`) |
| `meshgram/yaml_round_trip.py` | ruamel.yaml round-trip helpers shared by `migrate_config` and `config_export`: keep comments/quoting/indentation, quote strings PyYAML (YAML 1.1) would misread |
| `meshgram/migrate_config.py` | `python -m meshgram.migrate_config`: one-off merge of old `.env`/env settings into a copy of `config.yaml` (ruamel.yaml round-trip keeps comments); `ENV_SETTINGS` maps each legacy variable to its YAML path |
| `meshgram/types.py` | Event and action dataclasses; `Plugin` protocol; `PluginContext` (`status`, `web`) |
| `meshgram/status.py` | `StatusRegistry`: thread-safe connection status (radio, telegram, mqtt_publish, mqtt_subscribe, …) set by the app and plugins, shown by the web app; `remove(key)` |
| `meshgram/text_utils.py` | UTF-8 byte-aware chunking for radio MTU constraints (`split_for_mesh`) |

### Config

- **`config.yaml`** — the source of settings (besides the control panel's saved changes), secrets included (Telegram credentials, `meshcore` connection + bridge channel, name overrides, chunking params, `web`, plugins and their settings). Gitignored, kept out of the Docker image (`.dockerignore`) and bind-mounted read-only by `docker-compose.yml`. Never written by Meshgram.
- **`$MESHGRAM_DATA_DIR/plugins.json`** — plugin on/off state and settings changed in the control panel; wins over config.yaml for those plugins until reset there.
- **`$MESHGRAM_DATA_DIR/settings.json`** — every other setting changed in the control panel's Settings tab (dotted paths → values); wins over config.yaml from the next start until reset there. `telegram.bot_token`/`group_id` may live only here. The panel's Config file tab offers config.yaml with them written in; once config.yaml has them, they're dropped on startup.
- **`config.example.yaml`** — tracked template documenting every field; keep it in sync when adding settings (and the plugin's `settings_schema`, or `core_settings.SCHEMA`/`core_values` for settings outside the plugins).
- `web` falls back to the old `packet_map` plugin settings (`host`, `port`, `password`, `title`, `tile_url`, `tile_attribution`) while config.yaml has no `web` section.
- No env var overrides any setting. The only env vars are paths: `MESHGRAM_CONFIG_PATH` (config file) and `MESHGRAM_DATA_DIR` (persistent state). `dm_http_command` can still reference env vars explicitly (`${VAR}`, `auth.token_env`).
- `.env` is no longer loaded; `python -m meshgram.migrate_config` moves old `.env` settings into the YAML (writes `config.migrated.yaml`).

### Node name resolution

Channel messages carry the sender name in the text (`"<name>: <text>"`, split off by the transport). DMs resolve `contact_name_overrides` (config.yaml) → contact `adv_name` → pubkey prefix.

### Message chunking

`text_utils.split_for_mesh()` splits messages UTF-8 byte-aware to stay within radio MTU. Chunking config controls max bytes (channel messages are broadcasts: `broadcast_max_chunk_bytes`), inter-chunk delay, and retry backoff — all handled by the bridge and the action executor in `app.py`.
