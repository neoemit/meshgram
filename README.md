# Meshgram 🌐

Plugin-based bridge between a **mesh radio network** and **Telegram**.

Supports both **Meshtastic** and **MeshCore** radios. Speaks serial, TCP, and BLE. Runs on Linux, macOS, and Docker.

---

## ✨ Highlights

- 🔁 Bidirectional message bridge — Telegram ↔ mesh
- 🧵 Cross-platform reply linking (Meshtastic)
- ❤️ Bidirectional emoji reaction sync for linked messages (Meshtastic)
- ✂️ UTF-8 byte-aware chunking for long messages on radio MTU
- 🧩 Plugin architecture (`bridge`, `ping_pong`, `trace_me`, `dm_http_command`, `meshmapper`, `packet_map`)
- 🗺️ Optional MeshMapper observer uploads over MQTT (MeshCore)
- 📡 Optional live web map of packet propagation with a packet inspector (MeshCore)
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
| `MESHGRAM_DATA_DIR` | — | `data` (`/app/data` in Docker) | Directory for persistent state (`packet_map` history) |
| `SOLAR_HOST` / `SOLAR_TOKEN` / `SOLAR_API_KEY` | — | — | Examples for `dm_http_command` URL/auth templating |
| `MESHMAPPER_IATA` | — | from YAML | MeshMapper region code for the `meshmapper` plugin |
| `MESHMAPPER_PRIVATE_KEY` | — | — | Optional MeshCore private key (128 hex chars) for `meshmapper` token signing |
| `MESHMAPPER_SUBSCRIBE_USERNAME` / `MESHMAPPER_SUBSCRIBE_PASSWORD` | — | — | Optional MQTT subscriber account, so `meshmapper` can receive other observers' packets |
| `PACKET_MAP_HOST` / `PACKET_MAP_PORT` | — | from YAML | Listen address/port for the `packet_map` web app |
| `PACKET_MAP_PASSWORD` | — | — | Optional HTTP Basic auth password for the `packet_map` web app |
| `PACKET_MAP_DB_PATH` | — | from YAML | `packet_map` history database (relative to `MESHGRAM_DATA_DIR`) |

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

### `meshmapper` — MeshMapper observer (MQTT packet upload)

MeshCore only. Turns the radio connected to Meshgram into a [MeshMapper](https://meshmapper.net) **observer**: every RF packet the radio hears is uploaded to MeshMapper's MQTT broker so it can be plotted on your region's coverage map. It uses the same broker, topics, payload format and authentication as the observer clients listed in the [MeshMapper MQTT guide](https://wiki.meshmapper.net/mqtt-main/) (meshcoretomqtt, MeshCore-HA, pyMC), so MeshMapper processes it like any other observer.

The plugin runs alongside the bridge without affecting it: it only listens to the radio's raw RF log, sends nothing over the mesh or to Telegram, and if MeshMapper is unreachable the rest of Meshgram keeps working.

**Setup**

1. Find your **region code** on MeshMapper (usually a 3-letter IATA airport code such as `YOW`). It must match your MeshMapper region exactly, or the observer won't show up.
2. Run Meshgram with the MeshCore backend (`mesh.backend: meshcore` or `MESH_BACKEND=meshcore`).
3. Enable the plugin in `config.yaml`:

   ```yaml
   plugins:
     # ...your other plugins...
     - name: meshmapper
       enabled: true
       settings:
         iata: "YOW"        # your MeshMapper region code (or set MESHMAPPER_IATA in .env)
   ```

4. Restart Meshgram. You should see `MeshMapper: connecting to mqtt.meshmapper.net:443 …` and then `MeshMapper: connected; publishing packets to meshcore/YOW/<PUBLIC_KEY>/packets` in the logs.
5. Once the radio hears a packet, your node shows as **Online** with a MeshMapper broker badge under **Region → Observers** on the map.

The defaults already point at MeshMapper's broker. Everything else is optional:

| Setting | Default | Description |
|---|---|---|
| `iata` | — (**required**) | MeshMapper region code. `MESHMAPPER_IATA` overrides it. |
| `server` / `port` | `mqtt.meshmapper.net` / `443` | MQTT broker. Use these for a regional broker your MeshMapper admin has set up. |
| `transport` | `websockets` | `websockets` or `tcp` |
| `websocket_path` | `/` | WebSocket path on the broker |
| `tls` / `tls_verify` | `true` / `true` | TLS and certificate verification |
| `keepalive` | `60` | MQTT keepalive, in seconds |
| `token_audience` | same as `server` | `aud` claim in the auth token |
| `token_ttl_seconds` | `3600` | Auth token lifetime. Tokens are renewed 5 minutes before they expire. |
| `status_interval_seconds` | `300` | How often the retained `online` status is republished |
| `client_id_prefix` | `meshgram_` | MQTT client ID prefix (the public key is appended; max 23 chars) |
| `topic_status` / `topic_packets` | `meshcore/{IATA}/{PUBLIC_KEY}/status` / `…/packets` | Topic templates |
| `subscribe` | `true` | Also receive the packets other observers in your region upload, for the `packet_map` plugin. This needs a subscriber account. See **Other observers' packets** below. |
| `subscribe_username` / `subscribe_password` | — | Subscriber account for that. Prefer `MESHMAPPER_SUBSCRIBE_USERNAME` / `MESHMAPPER_SUBSCRIBE_PASSWORD` in `.env`, which override these. |
| `subscribe_server` / `subscribe_port` | same as `server` / `port` | Broker to subscribe on, if it's not the one you upload to (e.g. a regional broker) |
| `subscribe_transport` / `subscribe_tls` | same as `transport` / `tls` | Transport and TLS for the subscriber connection (`websocket_path` and `tls_verify` are shared) |
| `topic_subscribe` | `meshcore/{IATA}/+/packets` | Topic filter used for that |
| `private_key` | — | Optional. See **Authentication** below. `MESHMAPPER_PRIVATE_KEY` overrides it. |

**Authentication.** MeshMapper uses MeshCore's "device signing": the MQTT username is `v1_<PUBLIC_KEY>` and the password is a short-lived Ed25519-signed JWT (`publicKey`, `iat`, `exp`, `aud=mqtt.meshmapper.net`, `client`). By default Meshgram asks the **radio to sign** the token over the companion protocol, so the private key never leaves the device. If your companion firmware is too old to sign on the device, the log shows `could not create auth token`. You can then either update the firmware or set `MESHMAPPER_PRIVATE_KEY` to the radio's 64-byte private key, written as 128 hex characters. Meshgram checks that the key matches the connected radio before using it. Keep that key in `.env`, never in `config.yaml`.

**What gets published**

- `meshcore/<IATA>/<PUBLIC_KEY>/status` (retained): `{"status": "online", "timestamp", "origin" (radio name), "origin_id" (public key), "model", "firmware_version", "radio" ("freq,bw,sf,cr"), "client_version"}`. An `offline` status is registered as the MQTT last will and is also published on clean shutdown.
- `meshcore/<IATA>/<PUBLIC_KEY>/packets`: one message per received RF packet: `{"origin", "origin_id", "timestamp", "type": "PACKET", "direction": "rx", "time", "date", "len", "packet_type", "route" ("F" flood / "D" direct), "payload_len", "raw" (full packet hex), "SNR", "RSSI", "hash"}`, plus `"path"` for direct-routed packets. `hash` is computed exactly like the firmware's `Packet::calculatePacketHash`.

**Other observers' packets.** The plugin can also receive the packets other observers in your region upload (never your own) and hand them to the `packet_map` plugin. You then see packets your radio can't hear, routed to the observer that heard them.

This needs a **subscriber account**. MeshMapper's broker runs [meshcore-mqtt-broker](https://github.com/michaelhart/meshcore-mqtt-broker), which has two kinds of clients:

- *Publishers* log in with a device-signed token, like the uploader above. They may only publish to their own `meshcore/<IATA>/<PUBLIC_KEY>/…` topics, and the broker **closes the connection of a publisher that subscribes** to anything else.
- *Subscribers* log in with a username and password that the broker operator creates (roles: 1 = admin, 2 = full access, 3 = limited, with SNR/RSSI and some other fields removed). Only they can subscribe, e.g. to `meshcore/<IATA>/+/packets`.

So ask your MeshMapper region admin or the broker operator for a subscriber account, then set `MESHMAPPER_SUBSCRIBE_USERNAME` and `MESHMAPPER_SUBSCRIBE_PASSWORD` in `.env`. If it's on a different broker from the one you upload to (for example your region's own broker), also set `subscribe_server` / `subscribe_port`. The plugin then opens a second, read-only MQTT connection next to the upload connection and subscribes to `meshcore/<IATA>/+/packets`. The log says `subscribed to meshcore/<IATA>/+/packets` and then `receiving other observers' packets` when it works. A wrong username or password shows `subscriber login refused`. If the broker refuses the subscription, the log says so and Meshgram doesn't ask again until it restarts. Uploads are never affected. Without credentials, the plugin only uploads and logs once that it isn't receiving other observers' packets. Set `subscribe: false` to turn this off completely.

Notes:

- Only received packets are uploaded. The packet contents are the over-the-air bytes, so messages stay encrypted. Your radio's name, public key and radio settings are shared with MeshMapper, as with any observer.
- MeshMapper wants **fixed, always-on** observers. Mobile observers are strongly discouraged and may be dropped at ingest, so don't enable this on a radio that moves around.
- The plugin is ignored, with an error in the log, on the Meshtastic backend or when `iata` is missing. MeshMapper only accepts MeshCore data.
- It needs `paho-mqtt`, which is included in `requirements.txt`. If you installed Meshgram before this plugin existed, rerun `pip install -r requirements.txt` or rebuild the Docker image.

### `packet_map` — live packet propagation map (web app)

MeshCore only. Serves a small web app showing what your radio hears in real time:

- **Map (left):** every node that shares a GPS position, from adverts the radio hears and from its contact list. Each node type has its own shape and colour (repeaters ◆, room servers ■, companions ●, sensors ▲) and your own radio is pink. When a packet arrives, a glowing dot follows its real route (originator → each repeater in its path → your radio), pausing briefly at each relay, and the trail fades once it arrives. Hops that couldn't be placed on the map are drawn dotted. In the **Nodes on map** panel, click a node type to show or hide it (Alt-click shows only that type), and turn the live packet flow on or off.
- **Packet list (right):** every received RF packet, newest first. Each card shows the packet type, the sender, SNR/RSSI (colour-coded, with signal bars), hop count, the route as a chain of named stops, the channel and decrypted text for channel messages Meshgram has the key for, and a "Heard N×" badge when the same packet arrives over several paths. Click a card to see all decoded metadata (hash, route type, transport codes, advert key/position, source/destination hashes, raw hex), draw its route on the map with direction arrows, and replay its flow. You can search, filter by one or more packet types, and pause the list. If you scroll down, new packets don't move the list; a "new packets" button takes you back to the top.
- **Other MeshMapper observers:** with the `meshmapper` plugin enabled (and the broker allowing it), packets other observers in your region upload appear too. They carry a teal **MeshMapper** tag, their route ends at the observer that heard them, and on the map their flows are lighter and end with a teal ripple. Observers are drawn with a teal ring. A switch above the list shows **All**, **This radio** or **MeshMapper** packets, and a **MeshMapper packets** switch on the map turns their flows off. Channel messages in them are decrypted with your radio's channel keys, and adverts they carry add node positions. They're kept in a separate buffer (`max_remote_packets`), so a busy region can't push out your radio's own packets.
- **Messages (second tab):** a table of every channel message Meshgram could decrypt, newest first. Each row shows time, channel, sender, the text, the route (hops and last relay), the best SNR/RSSI and how many copies were heard. Copies of the same message heard over different paths are grouped into one row; expand it to see each path with its time offset and signal. **Replay** switches to the map and replays every path with its real timing (or one path from the expanded row), and a "Back to messages" link returns you to the same row. Columns are sortable, and you can search or filter by channel. While you're pointing at or tabbing through the table, new messages wait behind a "new messages" button so rows don't move under you. The tab shows a count of messages that arrived while you were on the map.
- **Connections (header):** a status dot for each service Meshgram connects to: the **radio**, the **Telegram** bot and, with the `meshmapper` plugin, the MeshMapper MQTT connections for **publishing** (MQTT ↑) and **subscribing** (MQTT ↓). Green is connected, amber is connecting, red is disconnected, and a hollow dot means turned off (for example no subscriber account). Click it for details: what each one is connected to, since when, and why it's down. If the page itself loses its connection to Meshgram, the dots turn hollow grey until it reconnects.
- The page has light and dark themes (following the system setting until you pick one), works on phones, and remembers the map position and zoom, hidden node types, table sort order, and theme in the browser across refreshes. The current tab is part of the URL (`#map` / `#messages`), so refresh and the browser's Back button work.

It only listens to the radio's raw RF log. It sends nothing over the mesh or to Telegram, and needs no extra Python packages. The page loads [Leaflet](https://leafletjs.com) from unpkg and map tiles from OpenStreetMap, so the browser needs internet access. Routes can only end at your radio on the map if it has a position: set one in the radio's advert settings (e.g. from the MeshCore app).

```yaml
plugins:
  - name: packet_map
    enabled: true
    settings:
      host: 127.0.0.1   # use 0.0.0.0 to reach it from other machines / Docker
      port: 8080
```

Then open `http://<host>:8080/`.

| Setting | Default | Description |
|---|---|---|
| `host` | `127.0.0.1` | Listen address. `PACKET_MAP_HOST` overrides it. |
| `port` | `8080` | Listen port. `PACKET_MAP_PORT` overrides it. |
| `password` | — | If set, the page requires HTTP Basic auth with this password (any username). Prefer `PACKET_MAP_PASSWORD` in `.env`. |
| `max_packets` | `500` | Packets kept in memory and sent to newly opened pages |
| `max_remote_packets` | `1000` | Packets from other MeshMapper observers kept in memory (see above) |
| `max_messages` | `1000` | Decrypted messages kept for the Messages tab. They're kept separately from `max_packets`, so they survive busy periods of adverts and ACKs. |
| `persist` | `true` | Save nodes and packet history to disk and restore them on restart (see **Persistence** below). Set `false` to keep everything in memory only. |
| `db_path` | `packet_map.sqlite3` | History database. A relative path is inside `MESHGRAM_DATA_DIR`. `PACKET_MAP_DB_PATH` overrides it. |
| `title` | `Meshgram` | Page title |
| `tile_url` / `tile_attribution` | OpenStreetMap | Leaflet tile layer URL template and attribution. When unset, OpenStreetMap tiles are restyled to match the light or dark theme; a custom tile server is shown as-is. |

Notes:

- **Docker:** set `host: 0.0.0.0` (or `PACKET_MAP_HOST=0.0.0.0`) and publish the port, e.g. add `ports: ["8080:8080"]` to the `meshgram` service.
- The page shows decrypted channel messages and node positions. Don't expose it on an untrusted network without `password` and, ideally, a TLS reverse proxy.
- Path hops are 1–3 byte public-key prefixes, so a hop is matched to a known node by prefix. If several repeaters share the prefix, the one closest to the next hop is picked. Unknown hops are listed by their hash and skipped on the map.
- For direct-routed packets the path is the remaining route, so it is drawn dashed and isn't connected to your radio.
- Nodes appear on the map once they share a position (in an advert or in the radio's contact list). The **Nodes on map** panel shows how many repeaters have no position.

**Persistence.** Nodes (names, types, positions, last heard/seen and signal), packets, other observers' packets and decrypted messages are saved to an SQLite database and restored on startup, so the map and lists come back after a restart, redeploy or reboot. Details:

- The database is `packet_map.sqlite3` in `MESHGRAM_DATA_DIR` (default `./data` next to `main.py`). It uses Python's built-in `sqlite3`, so there's nothing to install.
- Changes are written every 5 seconds in the background and once more on shutdown, in a single transaction each (WAL mode). A crash loses at most the last few seconds; the file is never left half written.
- The database holds what the page shows: `max_packets`, `max_remote_packets` and `max_messages` set how much history is kept on disk too. Older packets are deleted as new ones arrive, and lowering a limit trims the database on the next start. Nodes are kept.
- **Docker:** `docker-compose.yml` mounts a named volume, `meshgram-data`, at `/app/data`. It survives `docker compose up --build`, `down` and image rebuilds; only `docker compose down -v` (or `docker volume rm`) deletes it. To keep the file in a host folder instead, replace the volume with a bind mount such as `./data:/app/data`.
- **systemd:** the unit uses `StateDirectory=meshgram`, so the database is in `/var/lib/meshgram`.
- Back up the database with `sqlite3 packet_map.sqlite3 ".backup backup.sqlite3"` (safe while Meshgram is running). To start over, stop Meshgram and delete `packet_map.sqlite3*`.
- If the file is unreadable (for example corrupt), it's renamed to `packet_map.sqlite3.corrupt-<timestamp>` and a new one is started. If the directory isn't writable, the error is logged and the map runs without persistence.
- The database contains decrypted channel messages and node positions, like the page itself, so protect the data directory accordingly.

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

Coverage includes: config/env precedence, chunking (ASCII + emoji + long-token fallback), bridge filtering and reply mapping, Telegram + Meshtastic reaction parsing, ping keyword behavior, MeshCore trace-me responses, DM HTTP command, sender label resolution, MeshCore transport send/dispatch with a stubbed library, MeshMapper packet formatting / auth tokens / MQTT session handling with a fake broker client, packet map decoding / path resolution / HTTP + event stream.

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

### MeshMapper observer not showing up
- Check the logs for lines starting with `MeshMapper:`. `uploads disabled` explains why the plugin is inactive, for example a missing `iata` or the Meshtastic backend.
- `MQTT connect refused: Not authorized` means the broker rejected the token. Make sure the system clock is correct (the token has `iat`/`exp` timestamps) and that a configured `MESHMAPPER_PRIVATE_KEY` belongs to this radio.
- `could not create auth token` means the radio couldn't sign the token. Update the companion firmware or set `MESHMAPPER_PRIVATE_KEY`.
- The `iata` value must exactly match your MeshMapper region code.
- The observer only appears after the radio has heard at least one packet. Run with `LOG_LEVEL=DEBUG` to see each `MeshMapper: published packet …` line.
- No packets from other observers: receiving them needs a subscriber account (see **Other observers' packets**). Device-signed observer logins can only publish. The `packet_map` page's connection panel shows why **MQTT ↓** is off or down.

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
│   ├── meshcore_packets.py       # raw MeshCore RF packet decoding
│   ├── transport/
│   │   ├── __init__.py           # MeshTransport ABC + create_transport()
│   │   ├── meshtastic.py
│   │   └── meshcore.py
│   └── plugins/
│       ├── bridge.py
│       ├── ping_pong.py
│       ├── trace_me.py
│       ├── dm_http_command.py
│       ├── meshmapper.py         # MeshMapper MQTT observer uploads
│       ├── packet_map.py         # live packet map web app (server)
│       ├── packet_map_store.py   # packet map SQLite persistence
│       └── packet_map_static/    # packet map web app (page)
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
