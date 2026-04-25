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

Meshgram is a **plugin-based bidirectional bridge between Meshtastic (mesh radio) and Telegram**. It relays messages, replies, and emoji reactions across both platforms.

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

Each returns a list of `PluginAction` objects (`SendTelegramAction`, `SendMeshtasticAction`, `SendMeshtasticReactionAction`). New plugins are registered in `BUILTIN_PLUGINS` and enabled via `config.yaml`.

**Built-in plugins:**
- `plugins/bridge.py` — core relay with reply linking and reaction sync
- `plugins/ping_pong.py` — keyword-response automation with dedupe and channel filtering
- `plugins/dm_http_command.py` — DMs that invoke HTTP endpoints and return formatted responses

### Key modules

| File | Role |
|------|------|
| `meshgram/app.py` | MeshgramApp + MeshtasticClient; event dispatch; action execution |
| `meshgram/config.py` | Settings dataclasses; `load_settings()` with env-over-YAML precedence |
| `meshgram/types.py` | All event and action dataclasses; `Plugin` protocol; `PluginContext` |
| `meshgram/reply_links.py` | In-memory bidirectional Telegram↔Meshtastic message ID registry with TTL |
| `meshgram/text_utils.py` | UTF-8 byte-aware chunking for radio MTU constraints |

### Config

- **`.env`** — secrets (bot token, group ID, device path)
- **`config.yaml`** — runtime behavior (bridge channel, node name overrides, Telegram sender template, chunking params, plugin enable/disable + per-plugin settings)
- Env vars `MESH_MODE`, `MESH_HOST`, `MESH_PORT`, `MESH_DEVICE` override YAML connection settings at runtime

### Node name resolution

Sender display names resolve in order: `node_name_overrides` (config.yaml) → `shortName` → `longName` → normalized node ID.

### Message chunking

`text_utils.split_for_meshtastic()` splits messages UTF-8 byte-aware to stay within radio MTU. Chunking config controls max bytes, inter-chunk delay, retry backoff, and ACK wait behavior — all handled transparently by the action executor in `app.py`.
