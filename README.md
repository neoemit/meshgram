# Meshgram 🌐

Plugin-based bridge between a **mesh radio network** and **Telegram**.

Supports both **Meshtastic** and **MeshCore** radios. Speaks serial, TCP, and BLE. Runs on Linux, macOS, and Docker.

---

## ✨ Highlights

- 🔁 Bidirectional message bridge — Telegram ↔ mesh
- 🧵 Cross-platform reply linking (Meshtastic)
- ❤️ Bidirectional emoji reaction sync for linked messages (Meshtastic)
- ✂️ UTF-8 byte-aware chunking for long messages on radio MTU
- 🧩 Plugin architecture (`bridge`, `ping_pong`, `dm_http_command`)
- 🐳 First-class Docker deployment with platform-specific overlays
- 🛠️ Linux `systemd` service templates included

---

## 🧭 What Works Where

| Backend       | Serial / USB | TCP                          | BLE          |
|---------------|--------------|------------------------------|--------------|
| Meshtastic    | ✅ Linux, macOS, Docker (Linux only) | ✅ all platforms | ❌ not supported by bridge |
| MeshCore      | ✅ Linux, macOS, Docker (Linux only) | ✅ all platforms | ✅ Linux, macOS (host Python only — not Docker) |

**Platform notes:**
- **Linux:** every combination works, including USB passthrough into Docker.
- **macOS:** Docker Desktop **cannot** pass through USB or Bluetooth. Use bare-metal Python for serial/BLE, or run a host-side serial→TCP bridge (`socat`) and use the TCP overlay.
- **Docker:** USB passthrough requires Linux host. BLE never works in Docker.

**Backend differences:**
- MeshCore has **no packet-level reactions** — Telegram→MeshCore reaction actions are dropped silently.
- MeshCore has **no reply threading** — replies are forwarded as plain text.
- MeshCore identifiers are opaque strings, Meshtastic uses 32-bit packet IDs. The bridge handles both.

---

## 🚀 Quick Start

### 1. Get a Telegram bot

1. Talk to [@BotFather](https://t.me/BotFather), `/newbot`, save the token.
2. Add the bot to your group, make it admin (or at least allow it to read messages).
3. Get the group's chat ID — easiest way: forward a message from the group to [@userinfobot](https://t.me/userinfobot).

### 2. Clone and configure

```bash
git clone <this-repo> meshgram && cd meshgram
cp .env.example .env
# edit .env — set TELEGRAM_BOT_TOKEN, TELEGRAM_GROUP_ID, and your MESH_* vars
```

### 3. Pick how you want to run it

Jump to one of:
- 🐧 [Linux — bare-metal Python](#-linux--bare-metal-python)
- 🍎 [macOS — bare-metal Python](#-macos--bare-metal-python)
- 🐳 [Docker on Linux (USB serial)](#-docker-on-linux-usb-serial)
- 🐳 [Docker on macOS (TCP bridge)](#-docker-on-macos-tcp-bridge)
- 🧰 [Linux systemd service](#-linux-systemd-service)

---

## 🐧 Linux — Bare-Metal Python

Works for any backend, any transport (including BLE).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

**Backend selection** — set in `.env`:

```dotenv
# Meshtastic over USB serial
MESH_BACKEND=meshtastic
MESH_MODE=serial
MESH_DEVICE=/dev/ttyUSB0
```

```dotenv
# MeshCore over USB serial
MESH_BACKEND=meshcore
MESH_MODE=serial
MESH_DEVICE=/dev/ttyACM0
MESH_BAUDRATE=115200
```

```dotenv
# MeshCore over BLE (Linux/macOS, host Python only)
MESH_BACKEND=meshcore
MESH_MODE=ble
MESH_BLE_ADDRESS=12:34:56:78:90:AB
# MESH_BLE_PIN=123456     # if your companion needs pairing
```

```dotenv
# Either backend over TCP (e.g. Meshtastic node on the LAN)
MESH_BACKEND=meshtastic
MESH_MODE=tcp
MESH_HOST=192.168.1.50
MESH_PORT=4403
```

**Serial permissions** (one-time):
```bash
sudo usermod -aG dialout $USER
# log out / back in, or `newgrp dialout`
```

**Optional — stable device path with udev** (recommended when you have multiple USB devices):
```bash
# /etc/udev/rules.d/99-meshcore.rules
SUBSYSTEM=="tty", ATTRS{idVendor}=="303a", ATTRS{idProduct}=="1001", SYMLINK+="meshcore"
```
Then reload: `sudo udevadm control --reload && sudo udevadm trigger --action=add`. Use `MESH_DEVICE=/dev/meshcore`.

---

## 🍎 macOS — Bare-Metal Python

Same setup as Linux. Bare-metal is the recommended path on Mac because Docker Desktop can't see USB or Bluetooth.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

Find your serial device:
```bash
ls /dev/cu.usbmodem* /dev/cu.usbserial*
```

`.env` example:
```dotenv
MESH_BACKEND=meshcore
MESH_MODE=serial
MESH_DEVICE=/dev/cu.usbmodem34B7DA5AFD281
MESH_BAUDRATE=115200
```

For **BLE on macOS**, grant Bluetooth permission to your terminal: System Settings → Privacy & Security → Bluetooth → enable Terminal (or iTerm).

---

## 🐳 Docker on Linux (USB Serial)

This is the cleanest production path on Linux.

```bash
cp .env.example .env
# edit .env — make sure MESH_DEVICE points at your radio (e.g. /dev/ttyUSB0 or /dev/meshcore)

docker compose -f docker-compose.yml -f docker-compose.linux-serial.yml up --build -d
docker compose -f docker-compose.yml -f docker-compose.linux-serial.yml logs -f meshgram
```

The `linux-serial` overlay adds:
- `devices: [${MESH_DEVICE}:${MESH_DEVICE}]` — passes the USB serial device into the container
- `group_add: [dialout]` — grants the container access

**Tip — drop the `-f` flags:** copy the overlay to `docker-compose.override.yml` (auto-loaded by compose):
```bash
cp docker-compose.linux-serial.yml docker-compose.override.yml
echo "docker-compose.override.yml" >> .gitignore
docker compose up -d   # uses both files automatically
```

---

## 🐳 Docker on macOS (TCP Bridge)

Docker Desktop on macOS cannot pass `/dev/cu.*` devices into the container. Workaround: run `socat` on the host to expose the serial port as TCP, then point the container at it.

```bash
brew install socat

# In one terminal — keep running:
socat -d -d TCP-LISTEN:4403,reuseaddr,fork FILE:/dev/cu.usbmodem34B7DA5AFD281,raw,echo=0,b115200

# In another terminal:
docker compose -f docker-compose.yml -f docker-compose.macos-tcp.yml up --build -d
docker compose -f docker-compose.yml -f docker-compose.macos-tcp.yml logs -f meshgram
```

The `macos-tcp` overlay forces `MESH_MODE=tcp` with `MESH_HOST=host.docker.internal` and `MESH_PORT=4403`.

> ⚠️ This works for Meshtastic's serial wire protocol. For MeshCore companion radios over TCP, prefer running the companion's TCP firmware directly or use bare-metal Python instead.

---

## 🧰 Linux systemd Service

Use this when running on a Linux host without Docker.

```bash
# 1. Create service user + install location
sudo useradd --system --home /opt/meshgram --create-home --shell /usr/sbin/nologin meshgram
sudo mkdir -p /opt/meshgram
sudo chown -R meshgram:meshgram /opt/meshgram
# copy/clone the repo into /opt/meshgram

# 2. venv + dependencies
sudo -u meshgram bash -lc 'cd /opt/meshgram && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt'

# 3. Configure
sudo -u meshgram cp /opt/meshgram/deploy/systemd/meshgram.env.example /opt/meshgram/.env
sudo -u meshgram nano /opt/meshgram/.env
sudo -u meshgram nano /opt/meshgram/config.yaml

# 4. Install + enable unit
sudo cp /opt/meshgram/deploy/systemd/meshgram.service /etc/systemd/system/meshgram.service
sudo systemctl daemon-reload
sudo systemctl enable --now meshgram
sudo systemctl status meshgram --no-pager
```

Serial permissions:
```bash
sudo usermod -aG dialout meshgram
sudo systemctl restart meshgram
```

Logs / lifecycle:
```bash
journalctl -u meshgram -f
sudo systemctl restart meshgram
sudo systemctl stop meshgram
```

If your install path isn't `/opt/meshgram`, edit `WorkingDirectory`, `EnvironmentFile`, `ExecStart`, and `ReadWritePaths` in the unit file.

---

## 🔐 Environment Variables (`.env`)

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | — | Telegram bot token |
| `TELEGRAM_GROUP_ID` | ✅ | — | Telegram target chat/group ID |
| `MESH_BACKEND` | — | `meshtastic` | `meshtastic` or `meshcore` |
| `MESH_MODE` | — | from YAML | `serial`, `tcp`, or `ble` (BLE = meshcore only) |
| `MESH_DEVICE` | — | from YAML | Serial device path (e.g. `/dev/ttyUSB0`, `/dev/cu.usbmodemXXX`) |
| `MESH_BAUDRATE` | — | `115200` | Serial baudrate (MeshCore only; Meshtastic auto-negotiates) |
| `MESH_HOST` | — | from YAML | TCP host (use `host.docker.internal` on Docker Desktop) |
| `MESH_PORT` | — | `4403` (Meshtastic) / `5000` (MeshCore) | TCP port |
| `MESH_BLE_ADDRESS` | — | — | BLE MAC address (MeshCore + `MESH_MODE=ble`) |
| `MESH_BLE_PIN` | — | — | BLE pairing PIN (optional) |
| `MESH_NO_NODES` | — | `false` | Skip Meshtastic node DB download — improves resilience on proxied links |
| `MESHGRAM_CONFIG_PATH` | — | `config.yaml` | Path to YAML config |
| `LOG_LEVEL` | — | `INFO` | Python logging level |
| `SOLAR_HOST` / `SOLAR_TOKEN` / `SOLAR_API_KEY` | — | — | Examples for `dm_http_command` URL/auth templating |

Env vars override YAML for the same field.

---

## ⚙️ Configuration (`config.yaml`)

### Minimal example

```yaml
mesh:
  backend: meshtastic    # or "meshcore"

meshtastic:
  bridge_channel: 1
  connection:
    mode: tcp
    tcp_host: meshtastic.local
    tcp_port: 4403

telegram:
  include_captions: true
  sender_prefix_template: "[{display_name}] {message}"

plugins:
  - name: bridge
    enabled: true
    settings:
      channel: 1
```

### MeshCore example

```yaml
mesh:
  backend: meshcore

meshcore:
  bridge_channel: 0
  contact_name_overrides:
    "baad3b19": "Companion-1"
  connection:
    mode: serial
    serial_device: /dev/meshcore
    baudrate: 115200
    # tcp_host: localhost
    # tcp_port: 5000
    # ble_address: "12:34:56:78:90:AB"
    # ble_pin: "123456"
    auto_reconnect: true
```

### Full config reference

See [`config.yaml`](./config.yaml) in the repo — it contains every supported field with comments.

### Sender label resolution order

Meshtastic: `node_name_overrides` → `shortName` → `longName` → normalized node ID.
MeshCore: `contact_name_overrides[pubkey_prefix]` → contact `adv_name` → pubkey prefix.

### Chunking (relevant on both backends)

UTF-8 byte-aware splitting for radio MTU. Key knobs:
- `max_chunk_bytes` (default `160`) — hard cap per chunk
- `broadcast_max_chunk_bytes` (default `120`) — stricter cap for `^all` channels
- `inter_chunk_delay_ms` / `broadcast_min_inter_chunk_delay_ms` — spacing between chunks
- `retry_max_attempts`, `retry_initial_delay_ms`, `retry_backoff_factor` — retry policy
- `wait_for_ack` + `ack_timeout_ms` — gate next chunk on Meshtastic ACK (auto-skipped for broadcast and ignored on MeshCore)
- `abort_on_chunk_failure` — stop remaining chunks after terminal failure
- `payload_safety_margin_bytes` — reserve bytes below SDK max to avoid edge drops

---

## 🧩 Plugins

### `bridge` — Telegram ↔ Mesh relay

- Forwards text both directions, with sender prefix from `sender_prefix_template`
- Forwards Telegram media captions when `telegram.include_captions: true`
- Ignores bot-authored Telegram messages (loop prevention)
- Filters by channel: `bridge.settings.channel` (fallback to `<backend>.bridge_channel`)
- Reply linking (Meshtastic only): replies on either side map back to the original
- Reaction sync (Meshtastic only, requires `reactions_enabled: true`): linked messages only, first Unicode emoji, anonymous count fallback for Telegram

Settings:

```yaml
- name: bridge
  enabled: true
  settings:
    channel: 1
    reply_link_ttl_hours: 24
    reactions_enabled: true                      # Meshtastic only
    meshtastic_want_ack: true                    # Meshtastic only
    missing_target_policy: fallback_message
    reply_missing_suffix: "(reply target not found)"
    reaction_missing_notice_template: "(reaction target not found)"
```

### `ping_pong` — keyword auto-responder

- Replies to exact single-word keywords (case-insensitive, punctuation-stripped)
- Replies on the same channel the message came in on
- Per-channel allowlist via `channels`
- Message-ID dedupe for replayed packets (default behavior)
- Optional sender+keyword cooldown window for noisy networks

```yaml
- name: ping_pong
  enabled: true
  settings:
    keyword_responses:
      Ping: "Pong"
      Ack: "Ack"
    channels: [0, 1]
    response_dedupe_mode: packet_id_only        # or sender_keyword_window
    message_dedupe_ttl_seconds: 3600
    response_dedupe_ttl_seconds: 30             # used by sender_keyword_window mode
```

### `trace_me` — MeshCore route trace responder

MeshCore only. Replies to an exact channel message like `Trace` with the path hashes of repeaters that forwarded that message before it reached Meshgram:

```text
ff,2e,02 (3 hops)
```

```yaml
- name: trace_me              # `trace-me` is also accepted as an alias
  enabled: true
  settings:
    keywords: ["trace"]
    response_channel: same     # or a concrete MeshCore channel index, e.g. 0
    # channels: [0]            # optional incoming-channel allowlist
```

Notes:

- This plugin is ignored unless `mesh.backend: meshcore` / `MESH_BACKEND=meshcore`.
- MeshCore receive frames expose hop count metadata; repeater hash lists depend on MeshCore channel-log path enrichment. Meshgram enables channel-log decoding and refreshes channel metadata at startup so `meshcore_py` can correlate RF logs with received channel messages. When hashes are unavailable, the bot replies with the known hop count and `repeater list unavailable`.
- Path hashes are displayed in MeshCore's reported order and split according to path hash mode (1-, 2-, or 3-byte hashes).

### `dm_http_command` — DM → HTTP → DM reply

A node sends a single-word DM (e.g. `BATTERY`), the plugin fetches a configured HTTP endpoint, extracts a value, and DMs the formatted result back.

```yaml
- name: dm_http_command
  enabled: true
  settings:
    timeout_seconds: 8
    error_message: "Unable to fetch {command}"
    commands:
      BATTERY:
        url: "http://${SOLAR_HOST}/battery/"
        type: "json"           # or "text"
        value: "data.inv1.soc" # dot path; supports list indices
        msg: "{value}%"
        auth:
          type: bearer
          token_env: SOLAR_TOKEN
        headers:
          X-Api-Key: "${SOLAR_API_KEY}"
```

Env templating with `${VAR}` works in `url` and `headers`. Auth currently supports `bearer`.

---

## ⚠️ MeshCore Caveats

When `MESH_BACKEND=meshcore`:

- **No reactions** — Telegram reactions don't reach the radio; MeshCore packets never produce reaction events.
- **No reply threading** — `reply_id` is silently dropped; messages still send as plain text.
- **Opaque packet IDs** — internal IDs become strings (derived from MeshCore's `expected_ack` codes).
- **`meshtastic_want_ack` / `wait_for_ack`** — ignored by the MeshCore transport.
- **Echo suppression policy** — self-echo detection is identity-based; optional text fallback is configurable via `meshcore.outbound_echo_text_fallback_*`.

Everything else (channel routing, chunking, plugins, sender labels via `contact_name_overrides`) works the same.

---

## 🧪 Testing

```bash
.venv/bin/python -m unittest discover -s tests
```

Coverage includes: config/env precedence, chunking (ASCII + emoji + long-token fallback), bridge filtering and reply mapping, Telegram + Meshtastic reaction parsing, ping keyword behavior, MeshCore trace-me responses, DM HTTP command, sender label resolution, MeshCore transport send/dispatch with a stubbed library.

---

## 🩺 Troubleshooting

### Mesh connection fails
- Confirm `MESH_BACKEND`, `MESH_MODE`, and the matching `MESH_DEVICE` / `MESH_HOST` / `MESH_BLE_ADDRESS`.
- On Linux: `ls /dev/ttyUSB* /dev/ttyACM*` and check group access (`groups $USER` must include `dialout`).
- On macOS Docker: confirm `socat` is listening on the configured TCP port.
- In Docker: container needs `host.docker.internal` reachable (Docker Desktop only — on Linux Docker you may need `--add-host=host.docker.internal:host-gateway`).

### Telegram `409 Conflict` on polling
- Only one process can poll a given bot token. Stop the duplicate.

### Messages not bridging
- Check `TELEGRAM_GROUP_ID` matches the chat.
- Check `bridge.settings.channel` matches the radio channel index.
- Telegram side: ensure message has text or an enabled caption, and sender is not a bot.

### Long Telegram messages arrive partial on the radio
- Confirm `chunking.enabled: true`.
- Lower `max_chunk_bytes` (try `140`).
- For broadcast channels, lower `broadcast_max_chunk_bytes` and raise `broadcast_min_inter_chunk_delay_ms`.
- Watch logs for `Mesh send exhausted retries` and adjust retry settings.

### Reactions not syncing (Meshtastic)
- Confirm `bridge.settings.reactions_enabled: true`.
- Reactions only work on **already linked** messages — test by reacting to a message that was just bridged.
- For MeshCore: reactions are intentionally unsupported.

### Sender label shows the raw node ID
- Expected when peer metadata is missing.
- Set `meshtastic.node_name_overrides` or `meshcore.contact_name_overrides` for deterministic labels.

### MeshCore "could not open port"
- Verify the symlink/device path actually exists: `ls -l /dev/meshcore` (or whatever you set).
- For udev SYMLINK rules to fire, trigger an `add` action: `sudo udevadm trigger --action=add --sysname-match=ttyACM0`.

### Logs appear duplicated
- Check there's only one container (`docker ps -a`) and one Python process (`docker exec meshgram sh -c 'ls /proc | grep "^[0-9]*$"'`). If output is duplicated only in your terminal but the raw container log (`docker inspect <name> --format '{{.LogPath}}'`) shows one copy per line, it's a transient compose/terminal artifact — restart with `docker compose up -d` and re-attach with `docker compose logs -f`.

---

## 📁 Project Layout

```text
meshgram/
├── main.py                       # entrypoint
├── config.yaml                   # behavior config
├── .env.example                  # secrets + connection vars
├── Dockerfile
├── docker-compose.yml            # base
├── docker-compose.linux-serial.yml   # overlay — USB passthrough on Linux
├── docker-compose.macos-tcp.yml      # overlay — TCP via host socat on macOS
├── deploy/
│   └── systemd/
│       ├── meshgram.service
│       └── meshgram.env.example
├── meshgram/
│   ├── app.py
│   ├── config.py
│   ├── plugin.py
│   ├── reply_links.py
│   ├── text_utils.py
│   ├── types.py
│   ├── _mesh_helpers.py
│   ├── transport/
│   │   ├── __init__.py           # MeshTransport ABC + create_transport()
│   │   ├── meshtastic.py
│   │   └── meshcore.py
│   └── plugins/
│       ├── bridge.py
│       ├── ping_pong.py
│       └── dm_http_command.py
└── tests/
```

---

## 📘 Best Practices

- Keep secrets in `.env`, not `config.yaml`.
- Use `node_name_overrides` / `contact_name_overrides` for deterministic sender labels.
- Keep `dm_http_command` endpoints on trusted/internal networks.
- Use short, unambiguous single-word keys for DM commands.
- Only one polling process per bot token.
- On Linux Docker, use `docker-compose.override.yml` for your local overlay instead of long `-f` chains.

---

## 📜 License

See [`LICENSE`](./LICENSE).
