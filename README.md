# Meshgram 🌐

Plugin-based bridge between a **MeshCore mesh radio network** and **Telegram**, with a web app to watch the mesh and control the radio.

Talks to MeshCore companion radios over serial, TCP, and BLE. Runs on Linux, macOS, and Docker.

> Meshtastic support was removed. Meshgram now only works with MeshCore companion radios; a config that still selects Meshtastic is refused at startup with a hint.

---

## ✨ Highlights

- 🔁 Bidirectional message bridge — Telegram ↔ a radio channel
- ✂️ UTF-8 byte-aware chunking for long messages on radio MTU
- 🎛️ Web control panel: radio settings, channels (add them by name), contacts, and every plugin — turned on/off and configured live
- 🧩 Plugin architecture (`bridge`, `ping_pong`, `trace_me`, `dm_http_command`, `meshmapper`, `packet_map`)
- 🗺️ Optional MeshMapper observer uploads over MQTT
- 📡 Live web map of packet propagation with a packet inspector
- 🐳 First-class Docker deployment with platform-specific overlays
- 🛠️ Linux `systemd` service templates included

---

## 🧭 What Works Where

| Transport    | Linux | macOS | Docker |
|--------------|-------|-------|--------|
| Serial / USB | ✅ | ✅ | ✅ Linux hosts only |
| TCP          | ✅ | ✅ | ✅ |
| BLE          | ✅ | ✅ | ❌ |

**Platform notes:**
- **Linux:** every combination works, including USB passthrough into Docker.
- **macOS:** Docker Desktop **cannot** pass through USB or Bluetooth. Use bare-metal Python for serial/BLE, or run a host-side serial→TCP bridge (`socat`) and point Meshgram at it over TCP.
- **Docker:** USB passthrough requires a Linux host. BLE never works in Docker.

**MeshCore limits:** the protocol has no packet-level reactions and no reply threading, so the bridge relays messages only (a Telegram reply goes out as a plain message). Message identifiers are opaque strings.

---

## 🚀 Quick Start

### 1. Get a Telegram bot

1. Talk to [@BotFather](https://t.me/BotFather), `/newbot`, save the token.
2. Add the bot to your group, make it admin (or at least allow it to read messages).
3. Get the group's chat ID — easiest way: forward a message from the group to [@userinfobot](https://t.me/userinfobot).

### 2. Clone and configure

```bash
git clone <this-repo> meshgram && cd meshgram
cp config.example.yaml config.yaml && chmod 600 config.yaml
# edit config.yaml — set telegram.bot_token, telegram.group_id and meshcore.connection
```

Every setting lives in `config.yaml`, secrets included, so the file is gitignored. Upgrading from a version that used `.env`? See [Migrating from `.env`](#-migrating-from-env).

### 3. Pick how you want to run it

Jump to one of:
- 🐧 [Linux — bare-metal Python](#-linux--bare-metal-python)
- 🍎 [macOS — bare-metal Python](#-macos--bare-metal-python)
- 🐳 [Docker on Linux (USB serial)](#-docker-on-linux-usb-serial)
- 🐳 [Docker on macOS (TCP bridge)](#-docker-on-macos-tcp-bridge)
- 🧰 [Linux systemd service](#-linux-systemd-service)

---

## 🐧 Linux — Bare-Metal Python

Works for any transport (including BLE).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

**Radio connection** — set in `config.yaml`:

```yaml
# USB serial
meshcore:
  connection:
    mode: serial
    serial_device: /dev/ttyACM0
    baudrate: 115200
```

```yaml
# BLE (Linux/macOS, host Python only)
meshcore:
  connection:
    mode: ble
    ble_address: "12:34:56:78:90:AB"
    # ble_pin: "123456"      # if your companion needs pairing
```

```yaml
# TCP (a companion with Wi-Fi firmware on the LAN, or a serial→TCP bridge)
meshcore:
  connection:
    mode: tcp
    tcp_host: 192.168.1.50
    tcp_port: 5000
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
Then reload: `sudo udevadm control --reload && sudo udevadm trigger --action=add`. Use `serial_device: /dev/meshcore`.

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

`config.yaml` example:
```yaml
meshcore:
  connection:
    mode: serial
    serial_device: /dev/cu.usbmodem34B7DA5AFD281
    baudrate: 115200
```

For **BLE on macOS**, grant Bluetooth permission to your terminal: System Settings → Privacy & Security → Bluetooth → enable Terminal (or iTerm).

---

## 🐳 Docker on Linux (USB Serial)

This is the cleanest production path on Linux.

```bash
cp config.example.yaml config.yaml && chmod 600 config.yaml
# edit config.yaml — set serial_device to your radio (e.g. /dev/ttyUSB0 or /dev/meshcore)

docker compose -f docker-compose.yml -f docker-compose.linux-serial.yml up --build -d
docker compose -f docker-compose.yml -f docker-compose.linux-serial.yml logs -f meshgram
```

`docker-compose.yml` mounts `./config.yaml` into the container read-only; it isn't copied into the image. The `linux-serial` overlay adds:
- `devices: [${MESH_DEVICE:-/dev/ttyUSB0}:…]` — passes the USB serial device into the container. For another device, set `MESH_DEVICE` for Docker Compose, e.g. `export MESH_DEVICE=/dev/ttyACM0` or a one-line `.env` next to the compose files (Compose reads it to fill in `${…}`; Meshgram doesn't). It must match `serial_device` in `config.yaml`.
- `group_add: [dialout]` — grants the container access

**Tip — drop the `-f` flags:** copy the overlay to `docker-compose.override.yml` (auto-loaded by compose and gitignored), where you can also write the device path directly:
```bash
cp docker-compose.linux-serial.yml docker-compose.override.yml
docker compose up -d   # uses both files automatically
```

---

## 🐳 Docker on macOS (TCP Bridge)

Docker Desktop on macOS cannot pass `/dev/cu.*` devices into the container. Workaround: run `socat` on the host to expose the serial port as TCP, then point the container at it. MeshCore frames its companion protocol the same way over serial and TCP, so the bytes can be relayed as they are.

```bash
brew install socat

# In one terminal — keep running:
socat -d -d TCP-LISTEN:5000,reuseaddr,fork FILE:/dev/cu.usbmodem34B7DA5AFD281,raw,echo=0,b115200

# In another terminal:
docker compose up --build -d
docker compose logs -f meshgram
```

Point the connection at the host in `config.yaml`:

```yaml
meshcore:
  connection:
    mode: tcp
    tcp_host: host.docker.internal
    tcp_port: 5000
```

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

# 3. Configure (the file holds the bot token: keep it private to the service user)
sudo -u meshgram install -m 600 /opt/meshgram/config.example.yaml /opt/meshgram/config.yaml
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

If your install path isn't `/opt/meshgram`, edit `WorkingDirectory`, `MESHGRAM_CONFIG_PATH`, `ExecStart`, and `ReadWritePaths` in the unit file.

---

## 🔁 Migrating from `.env`

Older versions read settings from `.env` (and environment variables) as well as `config.yaml`, and the environment won. Now every setting, secrets included, lives in `config.yaml`, which git no longer tracks; `config.example.yaml` is the template. Meshgram ignores the old variables and logs a warning if any are still set. (Settings changed in the web app's control panel are the one exception: they're saved separately, see [Control panel](#-web-app-and-control-panel).)

`python -m meshgram.migrate_config` does the merge for you. It applies the old rules (environment over `.env` over YAML; `MESH_*` connection variables go to `meshcore.connection`), inlines the `${VAR}` and `token_env` references of `dm_http_command`, keeps your comments and layout, and writes the result to `config.migrated.yaml` (mode `600`) for you to review. It lists every value it moved and every `.env` entry it left out, and checks that Meshgram can load the result.

1. **Update without losing `config.yaml`.** Upstream stops tracking it, so a plain `git pull` would delete your copy. Move it aside first:
   ```bash
   mv config.yaml config.old.yaml && git pull && mv config.old.yaml config.yaml
   ```
   (Already pulled and it's gone? `git show ORIG_HEAD:config.yaml > config.yaml`.)
2. **Merge `.env` into it:**
   ```bash
   pip install -r requirements.txt          # adds ruamel.yaml
   python -m meshgram.migrate_config        # reads config.yaml + .env, writes config.migrated.yaml
   ```
   With Docker, run it in the image instead:
   ```bash
   docker compose build
   docker compose run --rm --no-deps --user "$(id -u):$(id -g)" -v "$PWD:/src" -w /src \
     -e PACKET_MAP_HOST=0.0.0.0 meshgram python -m meshgram.migrate_config
   ```
   `-e PACKET_MAP_HOST=0.0.0.0` carries over what the old `docker-compose.yml` set. `MESH_BACKEND=meshtastic` is refused: Meshtastic isn't supported any more.
3. **Switch over.** Review the file (and the comments the tool flags as still mentioning `.env`), then:
   ```bash
   mv config.migrated.yaml config.yaml
   docker compose up -d --build             # or restart however you run Meshgram
   rm .env                                  # once it runs
   ```
   systemd: reinstall `deploy/systemd/meshgram.service` (it no longer has `EnvironmentFile=`) and run `sudo systemctl daemon-reload`. Docker with the `linux-serial` overlay and a device other than `/dev/ttyUSB0`: keep `MESH_DEVICE=…` as the only line of `.env`, since Compose reads it.

Options: `--config` and `--env-file` pick the inputs, `-o` the output (`-` for stdout), `--force` overwrites it, and `--ignore-environment` reads `.env` only. Run `python -m meshgram.migrate_config --help` for details.

### Environment variables

Only two remain. They say where files are, not how Meshgram behaves, and nothing in `config.yaml` overrides them or is overridden by them:

| Variable | Default | Description |
|---|---|---|
| `MESHGRAM_CONFIG_PATH` | `config.yaml` | Path to the config file |
| `MESHGRAM_DATA_DIR` | `data` (`/app/data` in Docker, `/var/lib/meshgram` with systemd) | Directory for persistent state (`packet_map` history, plugin settings changed in the control panel) |

---

## ⚙️ Configuration (`config.yaml`)

### Minimal example

```yaml
telegram:
  bot_token: "123456789:ABCDEF_your_bot_token_here"
  group_id: -1001234567890
  include_captions: true
  sender_prefix_template: "[{display_name}] {message}"

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

web:
  host: 127.0.0.1    # 0.0.0.0 to reach it from other machines / Docker
  port: 8080
  # password: "…"    # needed for the control panel to change anything over the network

plugins:
  - name: bridge
    enabled: true
### Full config reference

See [`config.example.yaml`](./config.example.yaml) in the repo — it contains every supported field with comments.

### Sender label resolution order

Channel messages carry the sender's name in the text (`"<name>: <message>"`), which is used as is. Direct messages: `contact_name_overrides[pubkey_prefix]` → contact `adv_name` → pubkey prefix.

### Chunking

UTF-8 byte-aware splitting for radio MTU. Key knobs:
- `max_chunk_bytes` (default `160`) — hard cap per chunk
- `broadcast_max_chunk_bytes` (default `120`) — stricter cap for channel messages (broadcasts)
- `inter_chunk_delay_ms` / `broadcast_min_inter_chunk_delay_ms` — spacing between chunks
- `retry_max_attempts`, `retry_initial_delay_ms`, `retry_backoff_factor` — retry policy
- `abort_on_chunk_failure` — stop remaining chunks after terminal failure
- `payload_safety_margin_bytes` — reserve bytes below SDK max to avoid edge drops

---

## 🧩 Plugins

Plugins are listed in `config.yaml` and can also be turned on and off and reconfigured at runtime in the web app's [control panel](#-web-app-and-control-panel). Each built-in plugin describes its settings with a JSON Schema (`settings_schema`), from which the control panel builds its form.

### `bridge` — Telegram ↔ Mesh relay

- Forwards text both directions, with sender prefix from `sender_prefix_template`
- Forwards Telegram media captions when `telegram.include_captions: true`
- Ignores bot-authored Telegram messages (loop prevention)
- Filters by channel: `bridge.settings.channel` (fallback to `meshcore.bridge_channel`)

Settings:

```yaml
- name: bridge
  enabled: true
  settings:
    channel: 1
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

### `trace_me` — route trace responder

Replies to an exact channel message like `Trace` with the path hashes of repeaters that forwarded that message before it reached Meshgram:

```text
ff,2e,02 (3 hops)
```

```yaml
- name: trace_me              # `trace-me` is also accepted as an alias
  enabled: true
  settings:
    keywords: ["trace"]
    response_channel: same     # or a channel index, e.g. 0
    # channels: [0]            # optional incoming-channel allowlist
```

Notes:

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
        url: "http://192.168.0.10/battery/"
        type: "json"           # or "text"
        value: "data.inv1.soc" # dot path; supports list indices
        msg: "{value}%"
        auth:
          type: bearer
          token: "replace_me"  # or token_env: SOLAR_TOKEN
        headers:
          X-Api-Key: "replace_me"
```

Auth currently supports `bearer`, with the token in `auth.token`. To keep a value out of `config.yaml`, reference an environment variable instead: `${VAR}` in `url` and `headers`, `auth.token_env: VAR` for the token. Meshgram reads those from its own environment when it runs the command (it doesn't load `.env`).

### `meshmapper` — MeshMapper observer (MQTT packet upload)

Turns the radio connected to Meshgram into a [MeshMapper](https://meshmapper.net) **observer**: every RF packet the radio hears is uploaded to MeshMapper's MQTT broker so it can be plotted on your region's coverage map. It uses the same broker, topics, payload format and authentication as the observer clients listed in the [MeshMapper MQTT guide](https://wiki.meshmapper.net/mqtt-main/) (meshcoretomqtt, MeshCore-HA, pyMC), so MeshMapper processes it like any other observer.

The plugin runs alongside the bridge without affecting it: it only listens to the radio's raw RF log, sends nothing over the mesh or to Telegram, and if MeshMapper is unreachable the rest of Meshgram keeps working.

**Setup**

1. Find your **region code** on MeshMapper (usually a 3-letter IATA airport code such as `YOW`). It must match your MeshMapper region exactly, or the observer won't show up.
2. Enable the plugin in `config.yaml` (or in the control panel's **Plugins** tab):

   ```yaml
   plugins:
     # ...your other plugins...
     - name: meshmapper
       enabled: true
       settings:
         iata: "YOW"        # your MeshMapper region code
   ```

3. Restart Meshgram (not needed when you turn it on in the control panel). You should see `MeshMapper: connecting to mqtt.meshmapper.net:443 …` and then `MeshMapper: connected; publishing packets to meshcore/YOW/<PUBLIC_KEY>/packets` in the logs.
4. Once the radio hears a packet, your node shows as **Online** with a MeshMapper broker badge under **Region → Observers** on the map.

The defaults already point at MeshMapper's broker. Everything else is optional:

| Setting | Default | Description |
|---|---|---|
| `iata` | — (**required**) | MeshMapper region code |
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
| `subscribe` | `true` | Also receive the packets other observers in your region upload, for the `packet_map` plugin (live feed, plus MQTT with a subscriber account). See **Other observers' packets** below. `false` turns both off. |
| `live_feed` | `true` | Receive them from MeshMapper's public live feed. Needs no account. |
| `live_feed_url` | `wss://analyzer.meshmapper.net/ws` | Live feed WebSocket |
| `subscribe_username` / `subscribe_password` | — | Optional MQTT subscriber account (see **Other observers' packets**) |
| `subscribe_server` / `subscribe_port` | same as `server` / `port` | Broker to subscribe on, if it's not the one you upload to (e.g. a regional broker) |
| `subscribe_transport` / `subscribe_tls` | same as `transport` / `tls` | Transport and TLS for the subscriber connection (`websocket_path` and `tls_verify` are shared) |
| `topic_subscribe` | `meshcore/{IATA}/+/packets` | Topic filter used for that |
| `private_key` | — | Optional. See **Authentication** below. |

**Authentication.** MeshMapper uses MeshCore's "device signing": the MQTT username is `v1_<PUBLIC_KEY>` and the password is a short-lived Ed25519-signed JWT (`publicKey`, `iat`, `exp`, `aud=mqtt.meshmapper.net`, `client`). By default Meshgram asks the **radio to sign** the token over the companion protocol, so the private key never leaves the device. If your companion firmware is too old to sign on the device, the log shows `could not create auth token`. You can then either update the firmware or set `private_key` to the radio's 64-byte private key, written as 128 hex characters. Meshgram checks that the key matches the connected radio before using it. Anyone with the key can impersonate the radio, so keep `config.yaml` private (`chmod 600`).

**What gets published**

- `meshcore/<IATA>/<PUBLIC_KEY>/status` (retained): `{"status": "online", "timestamp", "origin" (radio name), "origin_id" (public key), "model", "firmware_version", "radio" ("freq,bw,sf,cr"), "client_version"}`. An `offline` status is registered as the MQTT last will and is also published on clean shutdown.
- `meshcore/<IATA>/<PUBLIC_KEY>/packets`: one message per received RF packet: `{"origin", "origin_id", "timestamp", "type": "PACKET", "direction": "rx", "time", "date", "len", "packet_type", "route" ("F" flood / "D" direct), "payload_len", "raw" (full packet hex), "SNR", "RSSI", "hash"}`, plus `"path"` for direct-routed packets. `hash` is computed exactly like the firmware's `Packet::calculatePacketHash`.

**Other observers' packets.** The plugin also receives the packets other observers in your region upload (never your own) and hands them to the `packet_map` plugin. You then see packets your radio can't hear, routed to the observer that heard them. There are two sources:

- **The live feed** (default, no account needed). This is the public WebSocket behind the **Visualize Live** view of MeshMapper's region maps (`wss://analyzer.meshmapper.net/ws`). Meshgram subscribes to your region only and follows the rules of MeshMapper's own web client: a keep-alive ping every 30 s, a reconnect after 65 s of silence, and exponential backoff with jitter, longest when the feed is shedding load. Each observation has the packet's hash, type, route, path, observer and signal, and the sender when MeshMapper knows it, but **not the packet bytes**. So channel messages from other observers can't be decrypted, and advert contents aren't shown. The log says `receiving region <IATA>'s packets from the live feed` when it's connected. Set `live_feed: false` to turn it off. The feed isn't a documented MeshMapper API, so it could change without notice.
- **An MQTT subscriber account** (optional), for the full packets. MeshMapper's broker runs [meshcore-mqtt-broker](https://github.com/michaelhart/meshcore-mqtt-broker), which has two kinds of clients:
  - *Publishers* log in with a device-signed token, like the uploader above. They may only publish to their own `meshcore/<IATA>/<PUBLIC_KEY>/…` topics, and the broker **closes the connection of a publisher that subscribes** to anything else.
  - *Subscribers* log in with a username and password that the broker operator creates (roles: 1 = admin, 2 = full access, 3 = limited, with SNR/RSSI and some other fields removed). Only they can subscribe, e.g. to `meshcore/<IATA>/+/packets`. A MeshMapper website or admin-panel login is *not* a broker account.

  If the broker operator gives you one, set `subscribe_username` and `subscribe_password` in the plugin settings. If it's on a different broker from the one you upload to (for example your region's own broker), also set `subscribe_server` / `subscribe_port`. The plugin then opens a second, read-only MQTT connection next to the upload connection and subscribes to `meshcore/<IATA>/+/packets`. The log says `subscribed to meshcore/<IATA>/+/packets` when it works. A wrong username or password shows `subscriber login refused` (the broker answers the same when the account is already at its connection limit). If the broker refuses the subscription, the log says so and Meshgram doesn't ask again until it restarts. While the MQTT subscription is up, the live feed stands by, so packets aren't shown twice.

Uploads are never affected by either. Set `subscribe: false` to stop receiving other observers' packets altogether.

Notes:

- Only received packets are uploaded. The packet contents are the over-the-air bytes, so messages stay encrypted. Your radio's name, public key and radio settings are shared with MeshMapper, as with any observer.
- MeshMapper wants **fixed, always-on** observers. Mobile observers are strongly discouraged and may be dropped at ingest, so don't enable this on a radio that moves around.
- The plugin is ignored, with an error in the log, when `iata` is missing.
- It needs `paho-mqtt` and `websockets` (for the live feed), which are included in `requirements.txt`. If you installed Meshgram before they were added, rerun `pip install -r requirements.txt` or rebuild the Docker image.

### `packet_map` — live packet propagation map

Feeds the web app's **Map** and **Messages** views with what your radio hears in real time (the web app itself is configured in the `web` section, see [Web app and control panel](#-web-app-and-control-panel)):

- **Map (left):** every node that shares a GPS position, from adverts the radio hears and from its contact list. Each node type has its own shape and colour (repeaters ◆, room servers ■, companions ●, sensors ▲) and your own radio is pink. When a packet arrives, a glowing dot follows its real route (originator → each repeater in its path → your radio), pausing briefly at each relay, and the trail fades once it arrives. Hops that couldn't be placed on the map are drawn dotted. In the **Nodes on map** panel, click a node type to show or hide it (Alt-click shows only that type), and turn the live packet flow on or off.
- **Packet list (right):** every received RF packet, newest first. Each card shows the packet type, the sender, SNR/RSSI (colour-coded, with signal bars), hop count, the route as a chain of named stops, the channel and decrypted text for channel messages Meshgram has the key for, and a "Heard N×" badge when the same packet arrives over several paths. Click a card to see all decoded metadata (hash, route type, transport codes, advert key/position, source/destination hashes, raw hex), draw its route on the map with direction arrows, and replay its flow. You can search, filter by one or more packet types, and pause the list. If you scroll down, new packets don't move the list; a "new packets" button takes you back to the top. On phones the type filters are one row you swipe sideways.
- **Hiding packet types:** types you never want to see (Acks, Paths, …) can be hidden altogether with the **Packet types shown** setting: untick them in the control panel (**Plugins → Packet map → Settings**, or the ⚙ chip at the start of the type filters, which also says how many are hidden). Hidden types leave the packet list, its filters, the header counts and the map's live flow, right away on every open page and without restarting anything. It's display only: they're still recorded, and tick a type again to bring its packets back. Nodes and the Messages tab aren't affected, and every payload type there is can be hidden, unassigned ones included.
- **Other MeshMapper observers:** with the `meshmapper` plugin enabled, packets other observers in your region upload appear too. They carry a teal **MeshMapper** tag, their route ends at the observer that heard them, and on the map their flows are lighter and end with a teal ripple. Observers are drawn with a teal ring. A switch above the list shows **All**, **This radio** or **MeshMapper** packets, and a **MeshMapper packets** switch on the map turns their flows off. When MeshMapper knows a packet's sender, it becomes the packet's origin, and a node your radio hasn't placed yet gets MeshMapper's name and position. Packets that arrive over MQTT (with a subscriber account) carry their bytes: channel messages in them are decrypted with your radio's channel keys, and adverts they carry add node positions. Live feed packets have no bytes, so they stay undecrypted. They're kept in a separate buffer (`max_remote_packets`), so a busy region can't push out your radio's own packets.
- **Messages (second tab):** a table of every channel message Meshgram could decrypt, newest first. Each row shows time, channel, sender, the text, the route (hops and last relay), the best SNR/RSSI and how many copies were heard. Copies of the same message heard over different paths are grouped into one row; expand it to see each path with its time offset and signal. **Replay** switches to the map and replays every path with its real timing (or one path from the expanded row), and a "Back to messages" link returns you to the same row. Columns are sortable, and you can search or filter by channel. While you're pointing at or tabbing through the table, new messages wait behind a "new messages" button so rows don't move under you. The tab shows a count of messages that arrived while you were on the map.

When the plugin is off, the Map and Messages views say so and link to the control panel's **Plugins** tab.

It only listens to the radio's raw RF log. It sends nothing over the mesh or to Telegram, and needs no extra Python packages. Routes can only end at your radio on the map if it has a position: set one in the control panel's **Radio** tab (or a MeshCore app).

```yaml
plugins:
  - name: packet_map
    enabled: true
```

| Setting | Default | Description |
|---|---|---|
| `hidden_packet_types` | none | Packet types left out of the page (see **Hiding packet types**): `ADVERT`, `GRP_TXT`, `GRP_DATA`, `TXT_MSG`, `ACK`, `REQ`, `RESPONSE`, `PATH`, `ANON_REQ`, `TRACE`, `MULTIPART`, `CONTROL`, `RAW_CUSTOM`, `TYPE_12`–`TYPE_14` |
| `max_packets` | `500` | Packets kept in memory and sent to newly opened pages |
| `max_remote_packets` | `1000` | Packets from other MeshMapper observers kept in memory (see above) |
| `max_messages` | `1000` | Decrypted messages kept for the Messages tab. They're kept separately from `max_packets`, so they survive busy periods of adverts and ACKs. |
| `persist` | `true` | Save nodes and packet history to disk and restore them on restart (see **Persistence** below). Set `false` to keep everything in memory only. |
| `db_path` | `packet_map.sqlite3` | History database. A relative path is inside `MESHGRAM_DATA_DIR`. |

Notes:

- `host`, `port`, `password`, `title`, `tile_url` and `tile_attribution` used to be `packet_map` settings. They're now in the `web` section; while config.yaml has no `web` section, Meshgram still reads them from here.
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

## 🎛️ Web App and Control Panel

Meshgram serves a web app (stdlib asyncio HTTP server, no extra packages) with three views: **Map** and **Messages** (fed by the `packet_map` plugin) and the **Control** panel. Open `http://<host>:<port>/`.

```yaml
web:
  enabled: true
  host: 127.0.0.1        # 0.0.0.0 to reach it from other machines; must be 0.0.0.0 in Docker
  port: 8080
  password: "…"          # HTTP Basic auth, any username
  # title: Meshgram
  # tile_url / tile_attribution: a custom Leaflet tile server (default: OpenStreetMap)
```

**Connections (header):** a status dot for each service Meshgram connects to: the **radio**, the **Telegram** bot and, with the `meshmapper` plugin, the MeshMapper MQTT connections for **publishing** (MQTT ↑) and **subscribing** (MQTT ↓), and the MeshMapper **live feed**. Green is connected, amber is connecting, red is disconnected, and a hollow dot means turned off. Click it for details. If the page loses its connection to Meshgram, the dots turn hollow grey until it reconnects.

**Control panel** (`#control`), in five tabs:

- **Radio** — status (name, public key, model and firmware, battery and storage, uptime, noise floor, last RSSI/SNR, airtime, packet counters, clock drift) and actions: send a zero-hop or flood advert, set the radio's clock from the computer, reboot. Forms for the name and position (and whether adverts share it), the LoRa parameters (frequency, bandwidth, spreading factor, coding rate) and TX power, contact auto-add, extra ACKs, telemetry sharing, the path hash size (on firmware that supports it), and the RX delay / airtime factor tuning.
- **Channels** — the channel slots with each channel's type, hash and which plugins use it. **Add a channel by name**: a hashtag channel (`#local`: the key comes from the name, so everyone who adds the same hashtag shares it), a private channel (paste the key someone shared, or leave it empty for a new random one), or the well-known Public channel. Rename, change a private channel's key, copy a key to share it, remove a channel, and send a message to a channel from the radio.
- **Contacts** — the radio's contacts with type, key, last advert and route; search and filter by type, reset a route (the next message floods to find a new one), or remove a contact.
- **Plugins** — every plugin with its state. Turn one on or off, or edit its settings in a form built from the plugin's schema. Saving applies them right away: a plugin that can take the change while running does (the Packet map's shown packet types, for example), the others restart with it. A link like `#control/plugins/packet_map` opens a plugin's settings directly. Secrets (passwords, tokens, API headers) are never sent to the browser: they show as "Saved", and are kept unless you type a new value.
- **Config file** — your `config.yaml` with the plugin changes made in the panel written into it, to make them the defaults. It lists which plugins differ, shows the changes as a diff (or the whole file), and has **Copy** and **Download** buttons. Only the changed plugins' `enabled` and `settings` are rewritten: comments, quoting and layout stay as they are, and the result is checked by loading it back the way Meshgram does. Replace `config.yaml` with it and restart Meshgram.

Changes to the radio are stored on the radio itself. Plugin changes are saved in `MESHGRAM_DATA_DIR/plugins.json` (mode `600`) and win over `config.yaml` until you click **Reset to config.yaml** on the plugin; a plugin changed this way is tagged *Changed here*. config.yaml is never rewritten (it holds secrets and Docker mounts it read-only); the **Config file** tab gives you the file to put in its place instead. Once `config.yaml` says the same as a saved change, Meshgram drops that change from `plugins.json` when it starts.

Without a browser, the same file comes from `GET /api/config.yaml`, for example on the Meshgram host:

```bash
curl -fsS -u ":$PASSWORD" http://127.0.0.1:8080/api/config.yaml -o config.new.yaml   # review, then: mv config.new.yaml config.yaml
```

**Security.**

- The control panel can reconfigure the radio, so it only changes anything when the web app has a **`password`** or listens on `127.0.0.1` only. Otherwise it's read-only and says so (the map and lists still work). Use a TLS reverse proxy when it's reachable beyond your LAN: Basic auth sends the password in the clear over plain HTTP.
- Changes must come from the page itself: requests that change something need a JSON body and a same-origin `Origin` / `Sec-Fetch-Site`, so another site can't submit them with your saved credentials (CSRF). The page can't be framed (`X-Frame-Options`, CSP `frame-ancestors`).
- The page shows decrypted channel messages, node positions and (with changes allowed) channel keys; protect it accordingly.
- The **Config file** tab hands out `config.yaml`, secrets included, so it follows the rules for changes: only with a password or on `127.0.0.1`, only to the page itself (or a client like `curl`), and without a password only when the page is opened as `localhost` / `127.0.0.1` (so a site whose name resolves to your machine can't read it).

The page loads [Leaflet](https://leafletjs.com) from unpkg and map tiles from OpenStreetMap, so the browser needs internet access. It has light and dark themes, works on phones, and remembers the map position, hidden node types, table sort order, theme and control panel tab across refreshes. The view is part of the URL (`#map`, `#messages`, `#control/channels`, …), so refresh and Back work.

**API.** The panel uses a small JSON API under `/api` (radio, channels, contacts, plugins) plus a Server-Sent Events stream at `/api/events`; see the docstring of `meshgram/web/api.py` for the endpoints.

---

## 🧪 Testing

```bash
.venv/bin/python -m unittest discover -s tests
```

Coverage includes: config loading and `.env` migration, chunking (ASCII + emoji + long-token fallback), bridge filtering and chunk sequencing, ping keyword behavior, trace-me responses, DM HTTP command, MeshCore transport send/dispatch with a stubbed library, MeshMapper packet formatting / auth tokens / MQTT session handling with a fake broker client, packet map decoding / path resolution / persistence, the web server (auth, CSRF and read-only rules, event stream), the control panel API, radio administration against a simulated radio, settings schemas and secret masking, runtime plugin management, and exporting config.yaml with the control panel's changes (comments kept, read back the way Meshgram loads it).

---

## 🩺 Troubleshooting

### Mesh connection fails
- Confirm `meshcore.connection` (`mode` and the matching `serial_device` / `tcp_host` / `ble_address`, and `baudrate` for serial).
- On Linux: `ls /dev/ttyUSB* /dev/ttyACM*` and check group access (`groups $USER` must include `dialout`).
- On macOS Docker: confirm `socat` is listening on the configured TCP port.
- In Docker: container needs `host.docker.internal` reachable (Docker Desktop only — on Linux Docker you may need `--add-host=host.docker.internal:host-gateway`).

### Telegram `409 Conflict` on polling
- Only one process can poll a given bot token. Stop the duplicate.

### Messages not bridging
- Check `telegram.group_id` matches the chat.
- Check `bridge.settings.channel` matches the radio channel index (the control panel's **Channels** tab shows which slot the bridge uses).
- Telegram side: ensure message has text or an enabled caption, and sender is not a bot.

### Long Telegram messages arrive partial on the radio
- Confirm `chunking.enabled: true`.
- Lower `max_chunk_bytes` (try `140`).
- For broadcast channels, lower `broadcast_max_chunk_bytes` and raise `broadcast_min_inter_chunk_delay_ms`.
- Watch logs for `Mesh send exhausted retries` and adjust retry settings.

### The control panel is read-only
- Set `web.password` (or listen on `127.0.0.1` only); the banner at the top of the panel says the same.

### A plugin ignores what's in `config.yaml`
- It was changed in the control panel: its card says *Changed here*. Click **Reset to config.yaml** in its settings, or delete its entry from `MESHGRAM_DATA_DIR/plugins.json` while Meshgram is stopped. To keep the change instead, put it in `config.yaml` from the **Config file** tab.

### Sender label shows the raw node ID
- Expected when peer metadata is missing.
- Set `meshcore.contact_name_overrides` for deterministic labels.

### MeshCore "could not open port"
- Verify the symlink/device path actually exists: `ls -l /dev/meshcore` (or whatever you set).
- For udev SYMLINK rules to fire, trigger an `add` action: `sudo udevadm trigger --action=add --sysname-match=ttyACM0`.

### MeshMapper observer not showing up
- Check the logs for lines starting with `MeshMapper:`. `uploads disabled` explains why the plugin is inactive, for example a missing `iata`.
- `MQTT connect refused: Not authorized` means the broker rejected the token. Make sure the system clock is correct (the token has `iat`/`exp` timestamps) and that a configured `private_key` belongs to this radio.
- `could not create auth token` means the radio couldn't sign the token. Update the companion firmware or set `private_key`.
- The `iata` value must exactly match your MeshMapper region code.
- The observer only appears after the radio has heard at least one packet. Set `runtime.log_level: DEBUG` to see each `MeshMapper: published packet …` line.
- No packets from other observers: check the **Live feed** dot in the web app's header (its panel says why it's down) and that `subscribe` / `live_feed` aren't `false`. Quiet regions can go minutes without a packet. **MQTT ↓** stays off without a subscriber account, which is fine. Device-signed observer logins can only publish (see **Other observers' packets**).

### Logs appear duplicated
- Check there's only one container (`docker ps -a`) and one Python process (`docker exec meshgram sh -c 'ls /proc | grep "^[0-9]*$"'`). If output is duplicated only in your terminal but the raw container log (`docker inspect <name> --format '{{.LogPath}}'`) shows one copy per line, it's a transient compose/terminal artifact — restart with `docker compose up -d` and re-attach with `docker compose logs -f`.

---

## 📁 Project Layout

```text
meshgram/
├── main.py                       # entrypoint
├── config.example.yaml           # every setting; copy to config.yaml (gitignored)
├── Dockerfile
├── docker-compose.yml            # base
├── docker-compose.linux-serial.yml   # overlay — USB passthrough on Linux
├── deploy/
│   └── systemd/
│       └── meshgram.service
├── meshgram/
│   ├── app.py
│   ├── config.py
│   ├── migrate_config.py         # one-off .env → config.yaml migration
│   ├── config_export.py          # config.yaml with the control panel's plugin changes
│   ├── yaml_round_trip.py        # editing YAML with its comments and layout kept
│   ├── plugin.py                 # BasePlugin, built-in plugin registry
│   ├── plugin_manager.py         # runs plugins; runtime on/off and settings (plugins.json)
│   ├── settings_schema.py        # JSON Schema subset: validation, secret masking
│   ├── radio_admin.py            # radio settings, channels and contacts
│   ├── status.py                 # connection status registry
│   ├── text_utils.py
│   ├── types.py
│   ├── meshcore_packets.py       # raw MeshCore RF packet decoding
│   ├── transport/
│   │   └── meshcore.py           # MeshCoreTransport
│   ├── web/
│   │   ├── server.py             # HTTP server, auth/CSRF, SSE event hub
│   │   ├── api.py                # control panel JSON API
│   │   └── static/               # the page (index.html) and control.js
│   └── plugins/
│       ├── bridge.py
│       ├── ping_pong.py
│       ├── trace_me.py
│       ├── dm_http_command.py
│       ├── meshmapper.py         # MeshMapper MQTT observer uploads
│       ├── packet_map.py         # map/messages data for the web app
│       └── packet_map_store.py   # packet map SQLite persistence
└── tests/
```

---

## 📘 Best Practices

- Keep `config.yaml` private (`chmod 600`): it holds the bot token and any plugin passwords. It's gitignored and kept out of the Docker image.
- Use `contact_name_overrides` for deterministic sender labels.
- Set `web.password` before exposing the web app; put it behind a TLS reverse proxy beyond your LAN.
- Keep `dm_http_command` endpoints on trusted/internal networks.
- Use short, unambiguous single-word keys for DM commands.
- Only one polling process per bot token.
- On Linux Docker, use `docker-compose.override.yml` for your local overlay instead of long `-f` chains.

---

## 📜 License

See [`LICENSE`](./LICENSE).
