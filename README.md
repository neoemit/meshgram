# Meshgram 🌐

Plugin-based bridge between a **MeshCore mesh radio network** and **Telegram**, with a web app to watch the mesh and control the radio.

Talks to MeshCore companion radios over serial, TCP, and BLE. Runs on Linux, macOS, and Docker.

> Meshtastic support was removed. Meshgram now only works with MeshCore companion radios; a config that still selects Meshtastic is refused at startup with a hint.

---

## ✨ Highlights

- 🔁 Bidirectional message bridge — Telegram ↔ a radio channel
- ✂️ UTF-8 byte-aware chunking for long messages on radio MTU
- 🎛️ Web control panel: radio settings, firmware updates (check, pick a version, install over USB), channels (add them by name), contacts, every plugin — turned on/off and configured live — and every other `config.yaml` setting (Telegram bot token and group, radio connection, web app, chunking, logging), applied with a restart from the panel
- 🧩 Plugin architecture (`bridge`, `ping_pong`, `trace_me`, `dm_http_command`, `meshmapper`, `letsmesh`, `meshrank`, `custom_mqtt`, `packet_map`, `check_in`)
- 🏅 Daily or weekly check-ins ("#MeshCoreMonday") with streaks, a leaderboard API and a [public leaderboard site](leaderboard/)
- 🗺️ Optional MQTT observer uploads to MeshMapper, LetsMesh and MeshRank; other observers' messages reach the plugins like the radio's
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

**Firmware updates from the control panel** work in Docker too. ESP32 boards stay on the same device while flashing. nRF52 boards (RAK4631, T-Echo, T1000-E, T114, …) drop off USB and come back as their bootloader, normally on the same `/dev/ttyACM*` device; if another USB serial device grabs that number meanwhile, the bootloader isn't visible inside the container. The overlay's commented `device_cgroup_rules` / `/dev` lines let the container see whichever device it comes back as.

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

Older versions read settings from `.env` (and environment variables) as well as `config.yaml`, and the environment won. Now every setting, secrets included, lives in `config.yaml`, which git no longer tracks; `config.example.yaml` is the template. Meshgram ignores the old variables and logs a warning if any are still set. (Settings changed in the web app's control panel are the one exception: they're saved separately and win over `config.yaml`, see [Control panel](#-web-app-and-control-panel).)

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
| `MESHGRAM_DATA_DIR` | `data` (`/app/data` in Docker, `/var/lib/meshgram` with systemd) | Directory for persistent state (`packet_map` history, settings changed in the control panel) |

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
| `subscribe_username` / `subscribe_password` | — | Optional MQTT subscriber account (see **Other observers' packets**, and [Receiving other observers' packets over MQTT](#receiving-other-observers-packets-over-mqtt)) |
| `subscribe_server` / `subscribe_port` | same as `server` / `port` | Broker to subscribe on, if it's not the one you upload to (e.g. a regional broker) |
| `subscribe_transport` / `subscribe_tls` | same as `transport` / `tls` | Transport and TLS for the subscriber connection (`websocket_path` and `tls_verify` are shared) |
| `topic_subscribe` | `meshcore/{IATA}/+/packets` | Topic filter used for that |
| `owner_email` / `owner_public_key` | — | Optional owner claims in the login token (your account's email; your companion's public key), as meshcore-packet-capture sends them. See [Attaching the observer to your account](#letsmesh-and-meshrank--more-mqtt-observers). |
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

### `letsmesh` and `meshrank` — more MQTT observers

Two more packet analyzers that take the same observer uploads (the same JSON as above), as their presets in [meshcoretomqtt](https://github.com/Cisien/meshcoretomqtt/tree/main/presets) and [meshcore-packet-capture](https://github.com/agessaman/meshcore-packet-capture/tree/main/presets) set them up. Turn on any mix of `meshmapper`, `letsmesh` and `meshrank` (in `config.yaml` or the control panel's **Plugins** tab). Each one uploads on its own, so a problem with one doesn't affect the others.

- **`letsmesh`** — [LetsMesh](https://letsmesh.net)'s packet analyzer. It has two brokers, `mqtt-us-v1.letsmesh.net` and `mqtt-eu-v1.letsmesh.net` (port 443, WebSockets + TLS), and the plugin uploads to the ones ticked under **Servers** (`servers: [us, eu]`, both by default). The login is the same device-signed token as MeshMapper's, with the broker as its audience, so the radio signs one per broker (or `private_key` is used, as above).
- **`meshrank`** — [MeshRank](https://meshrank.net). Its broker (`meshrank.net`, port 8883, MQTT over TLS) takes **anonymous** observers: data flows right away, the observer is known by the public key in its topics (`meshcore/<IATA>/<PUBLIC_KEY>/…`), and status documents aren't retained.

**Attaching the observer to your account.** Both networks do it the way meshcore-packet-capture does:

- **LetsMesh:** set `owner_email` to the email you log in to LetsMesh with (the forum account at [forum.letsmesh.net](https://forum.letsmesh.net)). The login token then carries it as its `email` claim, and the observer shows up in your LetsMesh dashboard. Optionally, set `owner_public_key` to your own companion's public key (64 hex characters). It goes in the token's `owner` claim, and LetsMesh shows that companion as the observer's owner. Both are sent only over verified TLS (the default), like meshcoretomqtt does. The log says `the login names the observer's owner (email …)`. MeshMapper takes the same two settings.
- **MeshRank:** log in at meshrank.net, open **Account → Observer Setup → MQTT observer registration** and click **Create registration key**. Set `registration_key` to that key. You can also paste the whole `packets = "meshrank/uplink/<KEY>/{PUBLIC_KEY}/packets"` line from the config MeshRank generates, and Meshgram picks the key out of it. The observer then uploads to `meshrank/uplink/<KEY>/<PUBLIC_KEY>/status` and `/packets`. It is still anonymous (the key is in the topic) and keeps its public-key identity. MeshRank marks it registered after ten minutes or 100 valid packets. The key works like a password, so it's masked in the control panel and kept out of the logs. *Earlier Meshgram versions asked for a MeshRank `username` / `password`. MeshRank doesn't use those: they're now ignored, with a warning in the log.*

```yaml
plugins:
  - name: letsmesh
    enabled: true
    settings:
      iata: "YOW"
      servers: [us, eu]
      owner_email: "you@example.com"     # your LetsMesh login: the observer shows up in your account
      # owner_public_key: ""             # optional: your companion's public key
  - name: meshrank
    enabled: true
    settings:
      iata: "YOW"
      # registration_key: ""             # from Account → Observer Setup (optional)
```

| Setting | Default | Plugins | Description |
|---|---|---|---|
| `iata` | — (**required**) | both | Region code |
| `servers` | `[us, eu]` | `letsmesh` | Which LetsMesh brokers to upload to |
| `owner_email` | — | `letsmesh` (and `meshmapper`) | Your account's email, sent in the login token: attaches the observer to your account |
| `owner_public_key` | — | `letsmesh` (and `meshmapper`) | Your companion's public key (64 hex), sent as the token's `owner` |
| `registration_key` | — | `meshrank` | MeshRank registration key (otherwise anonymous, unregistered) |
| `server` / `port` / `transport` | `meshrank.net` / `8883` / `tcp` | `meshrank` | Broker override |
| `status_interval_seconds` | `300` | both | How often the online status is republished |
| `subscribe` | `true` | both | Receive other observers' packets, when there's a subscriber account |
| `subscribe_username` / `subscribe_password` | — | both | MQTT subscriber account from the broker's operator. See [Receiving other observers' packets over MQTT](#receiving-other-observers-packets-over-mqtt) |
| `subscribe_server` / `subscribe_port` | the upload broker | both | Where the subscriber account logs in (`letsmesh`: the first server picked) |
| `private_key` | — | `letsmesh` | As for `meshmapper` |

Their connections appear in the web app's header and panel like MeshMapper's (`LetsMesh US MQTT (publish)`, `MeshRank MQTT (publish)`, …), and the log lines start with `LetsMesh US:`, `LetsMesh EU:` or `MeshRank:`.

### `custom_mqtt` — your own MQTT servers

The same observer uploads, to any MQTT brokers you choose: a club's or a region's, or one you host yourself. Add as many servers as you like under `servers`, each with a name, an address and a login (in the control panel: **Plugins → Custom MQTT servers → Add name**). Each server runs on its own, so one that's down or missing its password doesn't stop the others.

Unlike the analyzers above, a broker with a username and password usually lets the same login read too. So each server also subscribes to the region's packets (`meshcore/<IATA>/+/packets`) **with the same login**, and other observers' packets arrive tagged with the server's name (the packet list's **MQTT** view, the map, and every plugin, as in [Messages other observers heard](#messages-other-observers-heard)). An anonymous server reads anonymously. A device-signed (`token`) login needs a separate `subscribe_username` / `subscribe_password`.

```yaml
plugins:
  - name: custom_mqtt
    enabled: true
    settings:
      iata: "CPT"
      servers:
        ZR1RF:
          server: mqtt.zr1rf.za.net     # plain MQTT on 1883
          username: meshcoreuser
          password: "…"                 # or type it in the control panel
        Home:
          server: broker.lan
          tls: true                     # port 8883 by default then
```

| Setting | Default | Description |
|---|---|---|
| `iata` | — (**required**) | Region code, for every server |
| `servers.<name>.server` | — (**required**) | Host name or IP address. The name tags the packets received from it |
| `servers.<name>.port` | `1883`; `8883` with TLS; WebSockets `80` / `443` with TLS | Broker port |
| `servers.<name>.transport` / `websocket_path` | `tcp` / `/` | `tcp` or `websockets` |
| `servers.<name>.tls` / `tls_verify` | `false` / `true` | TLS, and whether its certificate is checked |
| `servers.<name>.auth` | `password` with a `username`, else `none` | `password`, `none` (anonymous) or `token` (signed by the radio, as on meshcore-mqtt-broker; `private_key` as for `meshmapper`) |
| `servers.<name>.username` / `password` | — | The login |
| `servers.<name>.subscribe` | `true` | Receive other observers' packets from this server |
| `servers.<name>.subscribe_username` / `subscribe_password` | the login | Only if reading needs another account |
| `servers.<name>.topic_packets` / `topic_status` / `topic_subscribe` | `meshcore/{IATA}/{PUBLIC_KEY}/packets` / `…/status` / `meshcore/{IATA}/+/packets` | For a broker with another topic layout |
| `servers.<name>.retain_status` | `true` | Retain the online/offline document |
| `status_interval_seconds` | `300` | How often the online status is republished |

Each server's connections show in the web app as `<name> MQTT (publish)` and `<name> MQTT (subscribe)` (folded into the header's **MQTT** dot), and its log lines start with `<name>:`. A server with a username but no password is shown as off, with `set the password for <username>`, until you set one.

### Receiving other observers' packets over MQTT

Uploading needs no account, but *reading* the packets other observers upload does, on every one of these networks. This guide covers the subscriber side of `meshmapper`, `letsmesh` and `meshrank` (`custom_mqtt` reads with its servers' own logins; see above).

**Why an observer login can't read.** An observer logs in as a *publisher*: with a device-signed token (MeshMapper, LetsMesh) or anonymously (MeshRank). Publishers may only write to their own topics. To read `meshcore/<IATA>/+/packets`, you need a *subscriber account*: a username and password that the broker's operator creates for you. Your website login is **not** a broker account. Neither is the LetsMesh email or MeshRank registration key above: those only attach your uploads to your account. How each broker treats a publisher that tries to read:

| Network | Broker | What an observer login gets when it subscribes | How to get read access |
|---|---|---|---|
| MeshMapper | meshcore-mqtt-broker | Connection closed ("Subscribe denied (publisher)") | Ask MeshMapper's admins for a subscriber account. Or use the free **live feed** (on by default, no packet bytes; see `meshmapper` above) |
| LetsMesh | meshcore-mqtt-broker | Connection closed, as on MeshMapper | Ask the LetsMesh team (e.g. on [forum.letsmesh.net](https://forum.letsmesh.net)) for a subscriber account. LetsMesh has no public feed to fall back on |
| MeshRank | its own | The subscription is *granted*, but no packets are ever delivered | Ask MeshRank's operator (meshrank.net) for an account with read access. Neither the anonymous preset nor a registration key can read |

None of the three hands out subscriber accounts by itself. They're created by hand by the broker's operator, if they agree to. Meshgram can't sign up for one for you, and doesn't try to subscribe with the observer login (on MeshMapper and LetsMesh, that would also drop the upload connection).

**Setting it up**, once the operator gives you a username and password:

1. In the control panel's **Plugins** tab, open the plugin (or edit `config.yaml`) and set:

   ```yaml
   - name: letsmesh            # or meshmapper / meshrank
     enabled: true
     settings:
       iata: "YOW"
       subscribe: true         # the default
       subscribe_username: "the-account-name"
       subscribe_password: "the-password"
       # Only if the operator says the account is on another broker / port:
       # subscribe_server: mqtt-eu-v1.letsmesh.net
       # subscribe_port: 443
       # subscribe_transport: websockets   # or tcp
       # subscribe_tls: true
   ```

   `letsmesh` reads from the first server ticked under **Servers** unless `subscribe_server` says otherwise. The account only needs to read `meshcore/<IATA>/+/packets` for your region (`topic_subscribe` changes the filter, if the operator uses another layout).
2. Save. The control panel applies it right away (restart Meshgram if you edited `config.yaml`). The plugin opens a second, read-only MQTT connection next to the upload one.
3. Check the **MQTT** dot in the web app's header. The panel lists `<Network> MQTT (subscribe)` for each plugin, and the log tells you where it stands:
   - `subscribed to meshcore/YOW/+/packets for other observers' packets`, then `receiving other observers' packets (first from …)`: it works.
   - `subscriber login refused …; check subscribe_username/subscribe_password`: wrong credentials, or the account is already connected as many times as it's allowed.
   - `the broker refused the subscription to …`: the account may not read that topic. Meshgram doesn't ask again until it restarts.
   - `subscribed to … but no packets from other observers in 10 minutes`: the broker took the subscription but sends nothing. That's what MeshRank does without read access. It also happens with an `iata` the network doesn't use, or in a very quiet region. The status says the same, and it clears with the first packet.
   - `Needs a subscriber account: …` (hollow dot): no account is set. Uploads are unaffected.

**What you get.** Packets from other observers carry their bytes, so the map shows them with their routes and adverts. Channel messages in them are decrypted with your radio's channel keys and reach every plugin (see the next section). Each packet counts once, however many observers or servers report it. Set `subscribe: false` to turn it all off. Uploads never depend on it.

### Messages other observers heard

Channel messages in the packets other observers upload are decrypted with your radio's channel keys and handed to **every plugin** like the ones the radio hears: the bridge relays them to Telegram, `ping_pong` and `trace_me` answer them (`trace_me` reports the route to the observer that heard it), `check_in` counts them, and so on. Plugins that care can tell them apart (`MeshTextEvent.source` is `"mqtt"`, and `raw_packet` names the `network` and observer). This needs packets with their bytes, so an MQTT subscriber account on one of the observer plugins (MeshMapper's live feed has no bytes). Each message is handled once however many observers (and your radio) heard it, and your own messages coming back through observers are ignored. Direct messages can't be read from other observers' packets (they're encrypted for the recipient), so they still need the radio. The same observer's packet arriving from two servers it uploads to counts once.

### `packet_map` — live packet propagation map

Feeds the web app's **Map** and **Messages** views with what your radio hears in real time (the web app itself is configured in the `web` section, see [Web app and control panel](#-web-app-and-control-panel)):

- **Map (left):** every node that shares a GPS position, from adverts the radio hears and from its contact list. Each node type has its own shape and colour (repeaters ◆, room servers ■, companions ●, sensors ▲) and your own radio is pink. When a packet arrives, a glowing dot follows its real route (originator → each repeater in its path → your radio), pausing briefly at each relay, and the trail fades once it arrives. The route starts at the sender whenever its position is known (from its adverts or your contacts); when only a short key prefix or the sender's name identifies it and several nodes match, the one nearest the first relay is taken (companions over repeaters for messages and requests). Hops that couldn't be placed on the map are drawn dotted. In the **Nodes on map** panel, click a node type to show or hide it (Alt-click shows only that type), and turn the live packet flow on or off.
- **Packet list (right):** every received RF packet, newest first. Each card shows the packet type, the sender, SNR/RSSI (colour-coded, with signal bars), hop count, the route as a chain of named stops, the channel and decrypted text for channel messages Meshgram has the key for, and a "Heard N×" badge when the same packet arrives over several paths. Click a card to see all decoded metadata (hash, route type, transport codes, advert key/position, source/destination hashes, raw hex), draw its route on the map with direction arrows, and replay its flow. You can search, filter by one or more packet types, and pause the list. If you scroll down, new packets don't move the list; a "new packets" button takes you back to the top. On phones the type filters are one row you swipe sideways.
- **Hiding packet types:** types you never want to see (Acks, Paths, …) can be hidden altogether with the **Packet types shown** setting: untick them in the control panel (**Plugins → Packet map → Settings**, or the ⚙ chip at the start of the type filters, which also says how many are hidden). Hidden types leave the packet list, its filters, the header counts and the map's live flow, right away on every open page and without restarting anything. It's display only: they're still recorded, and tick a type again to bring its packets back. Nodes and the Messages tab aren't affected, and every payload type there is can be hidden, unassigned ones included.
- **Other observers (MQTT):** with an MQTT observer plugin (`meshmapper`, `letsmesh`, `meshrank`, `custom_mqtt`) receiving other observers' packets, the packets other observers in your region upload appear too. They carry a teal tag naming the server they came through (**MeshMapper**, **LetsMesh**, **MeshRank**, or a `custom_mqtt` server's name), their route ends at the observer that heard them, and on the map their flows are lighter and end with a teal ripple. Observers are drawn with a teal ring. A switch above the list shows **All**, **Radio** or **MQTT** packets (every configured MQTT server's under **MQTT**), and an **MQTT packets** switch on the map turns their flows off. The same observer's packet reported by two servers is shown once. When MeshMapper knows a packet's sender, it becomes the packet's origin, and a node your radio hasn't placed yet gets MeshMapper's name and position. Packets that arrive over MQTT (with a subscriber account) carry their bytes: channel messages in them are decrypted with your radio's channel keys, and adverts they carry add node positions. Live feed packets have no bytes, so they stay undecrypted. They're kept in a separate buffer (`max_remote_packets`), so a busy region can't push out your radio's own packets.
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
| `max_remote_packets` | `1000` | Packets from other observers (MQTT) kept in memory (see above) |
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

### `check_in` — daily or weekly check-ins with streaks and a leaderboard

Gamifies a daily or weekly ritual: every Monday, say, nodes post `#MeshCoreMonday` on the Public channel. Check-ins are **weekly** (`frequency: weekly`, the default: once a week, on one `day` of the week) or **daily** (`frequency: daily`: once a day, any day). Any channel message on the check-in channel that **contains the keyword** (any case, anywhere in the text) on a **check-in day** is a check-in, whether your radio heard it or another observer did (over MQTT). The radio answers on the channel:

```text
📟 MeshCore Monday
🏅 LMB-T is #13 this week
🔥 Streak: 15
👥 13 participants
```

- **Weekly:** **rank** is the order of this week's first check-ins; the **streak** counts consecutive weeks with a check-in; **participants** is how many nodes have ever checked in (`{participants_this_period}` in a custom `reply_template` gives this week's). Weeks run Monday to Sunday in the configured `timezone`; on other days than `day` the keyword is ignored. A streak survives until a check-in day passes without a check-in.
- **Daily:** the same per day: rank today, a streak of consecutive days ("🏅 LMB-T is #3 today"), participants (ever, as for weekly ones). Days run midnight to midnight in `timezone`. Yesterday's streak holds all day today, and breaks once a day passes without a check-in.
- Checking in again in the same day or week gives the same rank with the current count. Changing `frequency` or `day` keeps the history: every check-in is recorded with its date, and counted by the new schedule from then on (a week with check-ins on several days is one weekly check-in).
- **Who checked in.** MeshCore channel messages carry the sender's *name* (`"LMB-T: …"`), not its key, so a name is matched to the one node that advertises it: adverts your radio hears, adverts in observers' packets, and the radio's contacts. A name no node has (yet), or that several nodes share, is counted under the name ("name only", **unverified** on the leaderboard) and linked to the node's key once its advert is heard: as soon as that name is the only one of its kind, or at the first **flood advert** of the name heard live, by your radio or an observer over MQTT, even if other nodes (old keys, contacts) went by it before; later check-ins under the name go to that node, whether your radio or only an observer heard them. A name several nodes share that checks in for the first time goes to the node heard flooding an advert of it last. Repeaters, room servers and sensors don't post on channels, so a node of the same name of those kinds (by its advert or contact entry) doesn't count. You can also link it by hand. A linked participant goes by its node's latest advertised name.
- **Observers.** With an MQTT observer plugin (`meshmapper`, `letsmesh`, `meshrank`) and a **subscriber account**, or a `custom_mqtt` server whose login can read, check-ins heard by other observers in your region count too (MeshMapper's free live feed has no message text, so it can't reveal them; see [Messages other observers heard](#messages-other-observers-heard)). They're decrypted with your radio's key for the channel. The same message heard by your radio and several observers counts, and is answered, once. Turn off `reply_to_observed` to count them without replying.
- **Control panel.** Besides the settings, the plugin's card has a **Check-ins** panel: everyone with streak, best streak, total, today's or this week's rank and last check-in; per participant, a grid of the last 26 weeks (or 28 days) to add or remove check-ins, set a streak, rename, link to a node, the check-ins on record, and delete. **Export…** saves a **backup** (a JSON file with every participant and every check-in, as they are) or a CSV **summary** (one row per node). **Import…** takes either: a backup brings everything back, here or in another Meshgram (*Add to what's here* skips what's already on record, so importing twice changes nothing; *Replace everything here* restores it exactly); summaries come from another check-in database. **Clear all…** deletes every participant and check-in, so the leaderboard starts from zero (names learned from adverts are kept); it offers to download a backup first.
- **Import format:** one record per node with its key (or a key prefix, or just a name), its current `streak`, and optionally its `total` check-ins and the date of its `last_check_in`. Each record gives the node `streak` consecutive daily or weekly check-ins (as the plugin counts them) up to that date (or the date you pick); `total` adds the earlier check-ins on top. CSV with a header row (`node_id,name,streak,total,last_check_in`) or JSON (`[{...}]` or `{"records": [...]}`). Preview first: nothing is imported while any record has a problem. *Replace* deletes everything first.
- **Leaderboard API.** `GET /api/plugins/check_in/leaderboard` returns the standings, best streak first: each node's `id` (public key), `hash` (first byte), `name`, `streak`, `best_streak`, `total_check_ins`, `last_check_in`, `checked_in_this_period`, `period_rank` and `resolved` (false for name-only participants), plus where to check in (`channel`: the channel's `name` and `kind`, `public`, `hashtag` or `private`, never its key; `null` until the radio's channels are read), `frequency` (`daily` or `weekly`) and `check_in_day` (weekly), the `period` under way (today, or this week's Monday), whether the check-in window is open (and when it opens or closes), the participant count so far (`participants_this_period`) and `meshgram_title` (the web app's `title`, which the leaderboard site shows as its name). No individual check-ins are exposed. The older field names (`week`, `check_in_days`, `participants_this_week`, `checked_in_this_week`, `week_rank`) are still sent for sites made before daily check-ins; see the [leaderboard site's README](leaderboard/README.md#what-the-leaderboard-contains). Set `api_token` and send `Authorization: Bearer <token>` to read it without the web app's password; with a token set, requests without it (or the password) are refused even when the web app has no password. The [leaderboard site](leaderboard/) shows it.
- **Persistence.** Participants, every check-in (with its date) and the names from adverts are kept in `check_in.sqlite3` in `MESHGRAM_DATA_DIR` (the `meshgram-data` volume in Docker), written as they happen. It survives restarts, rebuilds and `docker compose down` (not `down -v`). Back it up from the control panel (Export… → Download backup) or with `sqlite3 check_in.sqlite3 ".backup backup.sqlite3"`.

```yaml
plugins:
  - name: check_in
    enabled: true
    settings:
      keyword: "#MeshCoreMonday"
      frequency: weekly   # or daily
      day: 0              # Monday
      timezone: Europe/Rome
      api_token: "a-long-random-string"
```

| Setting | Default | Description |
|---|---|---|
| `channel` | `0` | Channel slot where check-ins are posted and answered |
| `keyword` | `#MeshCoreMonday` | Word or phrase a message must contain (any case) |
| `title` | `MeshCore Monday` | Heads the reply and the leaderboard; at most 20 characters |
| `frequency` | `weekly` | `weekly`: one check-in a week counts, on `day`; `daily`: one a day, any day. Streaks and ranks are per week or per day |
| `day` | `0` | Weekly check-ins only: the day of the week they count on, `0` = Monday … `6` = Sunday (or a name, `monday`) |
| `timezone` | `UTC` | IANA time zone where days and weeks begin |
| `reply` | `true` | Answer check-ins on the channel |
| `reply_to_observed` | `true` | Also answer check-ins only other observers heard |
| `reply_template` | the message above | Placeholders `{title}`, `{name}`, `{rank}`, `{this_period}` ("this week" or "today"), `{streak}`, `{streak_label}` ("15 weeks" or "15 days"; `{streak_weeks}` is its older name), `{participants}`, `{participants_label}` ("13 participants": every node that has ever checked in), `{participants_this_period}`, `{participants_this_period_label}` (only today's or this week's), `{total}`, `{keyword}`. A reply longer than one radio message (120 bytes with the default chunking settings) gets the name and title shortened (…) to fit; one that still doesn't fit is split between lines. The default reply always goes out as one message. |
| `reply_cooldown_seconds` | `30` | Check-ins from the same node sooner than this after a reply still count, unanswered |
| `api_token` | none | Bearer token for the leaderboard API (secret) |
| `db_path` | `check_in.sqlite3` | Database file; a relative path is inside `MESHGRAM_DATA_DIR` |

Everything but `db_path` changes without a restart.

The older `days` setting (a list of weekdays) still works while `day` isn't set: one day becomes `day`. Weekly check-ins have one day now, so a list of several days (or `[]`, any day) counts on the first of them (Monday for `[]`), with a warning in the log; set `day`, or `frequency: daily`, instead.

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

**Connections (header):** a status dot for each service Meshgram connects to: the **radio**, the **Telegram** bot and, with the MQTT observer plugins, one **MQTT** dot for all their connections (green when every one that's on is connected, red when one is down); the panel lists each one: publishing and subscribing per server, and MeshMapper's **live feed**. Green is connected, amber is connecting, red is disconnected, and a hollow dot means turned off. Click it for details. If the page loses its connection to Meshgram, the dots turn hollow grey until it reconnects.

**Control panel** (`#control`), in six tabs:

- **Radio** — status (name, public key, model and firmware, battery and storage, uptime, noise floor, last RSSI/SNR, airtime, packet counters, clock drift) and actions: send a zero-hop or flood advert, set the radio's clock from the computer, reboot. Forms for the name and position (and whether adverts share it), the LoRa parameters (frequency, bandwidth, spreading factor, coding rate) and TX power, contact auto-add, extra ACKs, telemetry sharing, the path hash size (on firmware that supports it), and the RX delay / airtime factor tuning. A **Firmware** card checks for and installs MeshCore updates (see [Firmware updates](#firmware-updates)).
- **Channels** — the channel slots with each channel's type, hash and which plugins use it. **Add a channel by name**: a hashtag channel (`#local`: the key comes from the name, so everyone who adds the same hashtag shares it), a private channel (paste the key someone shared, or leave it empty for a new random one), or the well-known Public channel. Rename, change a private channel's key, copy a key to share it, remove a channel, and send a message to a channel from the radio.
- **Contacts** — the radio's contacts with type, key, last advert and route; search and filter by type, reset a route (the next message floods to find a new one), or remove a contact.
- **Plugins** — every plugin with its state. Turn one on or off, or edit its settings in a form built from the plugin's schema (a plugin may add a panel of its own, like the check-ins one). Saving applies them right away: a plugin that can take the change while running does (the Packet map's shown packet types, for example), the others restart with it. A link like `#control/plugins/packet_map` opens a plugin's settings directly. Secrets (passwords, tokens, API headers) are never sent to the browser: they show as "Saved", and are kept unless you type a new value.
- **Settings** — everything else in `config.yaml`, one card per section: **Telegram** (bot token, group ID, captions, sender prefix), **Radio** (connection: serial device, baud rate, TCP host/port or Bluetooth address/PIN; bridge channel, contact names, echo fallback), **Web app** (address, port, password, title, map tiles), **Message chunking** and **Logging**. Serial devices found on the machine are suggested. A new bot token or group is checked with Telegram before it's saved. These settings are read when Meshgram starts, so a saved change shows *Restart to apply* until the **Restart now** button (or a restart of the service/container) brings Meshgram back with it; the page waits for it and reloads. Secrets (bot token, web password, Bluetooth PIN) are never sent to the browser.
- **Config file** — your `config.yaml` with the settings and plugin changes made in the panel written into it, to make them the defaults. It lists which settings and plugins differ, shows the changes as a diff (or the whole file), and has **Copy** and **Download** buttons. Only what changed is rewritten: comments, quoting and layout stay as they are, and the result is checked by loading it back the way Meshgram does. Replace `config.yaml` with it and restart Meshgram.

### Firmware updates

The **Radio** tab's **Firmware** card shows the installed MeshCore version and the newest release for the radio's board, and installs any published version (the latest is picked; older ones and pre-releases can be chosen too). Releases are the official MeshCore companion firmware from [GitHub](https://github.com/meshcore-dev/MeshCore/releases), the same files the MeshCore web flasher uses; the list is fetched when the card is opened and kept for an hour (**Check for updates** fetches it again).

Click **Update to v…**, confirm, and Meshgram:

1. downloads the file and checks it against the checksum GitHub publishes for it, then checks it really is an update for this kind of board (an ESP32 application image for the radio's chip, or an nRF52 application-only DFU package);
2. disconnects from the radio and stops reconnecting, so the USB serial port is free;
3. puts the radio into its bootloader and installs the update **without erasing anything**: the radio keeps its settings, identity (keys), contacts and channels. ESP32 boards are reset into their ROM bootloader by esptool and get the new application written over the app partition named in their own partition table; nRF52 boards get the 1200-baud "touch" that starts their Adafruit bootloader, then a DFU transfer of the application (the bootloader and SoftDevice aren't touched). A transfer that fails is tried again;
4. lets the radio restart, reconnects, and does what it does after any reconnect: channels and contacts are read again and every plugin is told the radio is back;
5. checks the radio now reports the new version and still has the same identity.

Progress shows on every open page, step by step with a log. It takes a minute or two; Meshgram's radio features (bridge, observers, map) pause meanwhile, and Meshgram can't be restarted from the panel until it's done.

What it needs:

- **The radio on USB** (`meshcore.connection.mode: serial`). Over TCP or Bluetooth, use the [MeshCore web flasher](https://flasher.meshcore.co.uk) instead. The update installs the USB companion firmware (`companion_radio_usb`), the one Meshgram talks to.
- **The board.** The radio reports a model ("Heltec V3"); when that matches one board, it's picked. Some models come in variants (Heltec V3 or Wireless Stick Lite V3, T114 with or without display, …): pick yours once and it's remembered. An update for a board the radio's model doesn't match is refused.
- **ESP32 or nRF52.** RP2040 boards update by copying the `.uf2` file to the drive they show in BOOTSEL mode, STM32 boards with an STM32 programmer.
- **Internet access** to GitHub, and the flashing tools `esptool` and `adafruit-nrfutil` (in `requirements.txt`).

Choosing an older version than the installed one asks for an extra confirmation: older firmware may not understand settings saved by newer versions. If the radio isn't connected (a previous update stopped half-way, say), the card can still install firmware on the board you pick, without the checks against what the radio reports.

Changes to the radio are stored on the radio itself. Plugin changes are saved in `MESHGRAM_DATA_DIR/plugins.json`, other settings in `MESHGRAM_DATA_DIR/settings.json` (both mode `600`, as they may hold secrets), and they win over `config.yaml` until you click **Reset to config.yaml** on the plugin or settings section; what's changed this way is tagged *Changed here*. config.yaml is never rewritten (it holds secrets and Docker mounts it read-only); the **Config file** tab gives you the file to put in its place instead. Once `config.yaml` says the same as a saved change, Meshgram drops that change when it starts.

`telegram.bot_token` and `telegram.group_id` may even be left out of `config.yaml` when they're set in the panel (`settings.json` has them then). Meshgram can't start without them, though, so a fresh install still needs them in `config.yaml` once to reach the panel.

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

**API.** The panel uses a small JSON API under `/api` (radio, firmware, channels, contacts, plugins, settings, restart) plus a Server-Sent Events stream at `/api/events`; see the docstring of `meshgram/web/api.py` for the endpoints. Plugins can add routes of their own while they run (the check-in plugin's are listed in `meshgram/plugins/check_in_api.py`).

---

## 🧪 Testing

```bash
.venv/bin/python -m unittest discover -s tests
```

Coverage includes: config loading and `.env` migration, chunking (ASCII + emoji + long-token fallback), bridge filtering and chunk sequencing, ping keyword behavior, trace-me responses, DM HTTP command, MeshCore transport send/dispatch with a stubbed library, MQTT observer packet formatting / auth tokens / MQTT sessions (MeshMapper, LetsMesh, MeshRank) with a fake broker client, observers' channel messages reaching the plugins, packet map decoding / path resolution / persistence, the web server (auth, CSRF and read-only rules, event stream), the control panel API, radio administration against a simulated radio, firmware updates (release parsing, board matching, checksum/image/partition checks, the update flow and its API with a simulated radio and flashing tool), settings schemas and secret masking, runtime plugin management, and exporting config.yaml with the control panel's changes (comments kept, read back the way Meshgram loads it).

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

### A setting ignores what's in `config.yaml`
- It was changed in the control panel's **Settings** tab: its section says *Changed here*. Click **Reset to config.yaml** there and restart, or delete its entry from `MESHGRAM_DATA_DIR/settings.json` while Meshgram is stopped (also the way back if a change there keeps Meshgram from starting or the web app from being reachable).

### Sender label shows the raw node ID
- Expected when peer metadata is missing.
- Set `meshcore.contact_name_overrides` for deterministic labels.

### A firmware update fails
- The card's **Details** log shows each step. Nothing is erased, so it's safe to try again.
- *"Couldn't put the radio into its bootloader"* (ESP32): hold the radio's **BOOT** button, press and release **RESET**, let go of BOOT, and try again; the update then starts from the bootloader.
- An nRF52 radio left in its bootloader (its LED pulses, it doesn't reconnect): try the update again, or double-press its reset button first.
- *"The radio didn't come back on USB"* in Docker: see the firmware note under [Docker on Linux](#-docker-on-linux-usb-serial).
- *"…hasn't reconnected yet"*: the update was written; unplug the radio and plug it in again. Meshgram keeps trying to reconnect.

### MeshCore "could not open port"
- Verify the symlink/device path actually exists: `ls -l /dev/meshcore` (or whatever you set).
- For udev SYMLINK rules to fire, trigger an `add` action: `sudo udevadm trigger --action=add --sysname-match=ttyACM0`.

### MQTT observer (MeshMapper, LetsMesh, MeshRank) not showing up
- Check the logs for lines starting with `MeshMapper:`, `LetsMesh US:`, `LetsMesh EU:` or `MeshRank:`. `uploads disabled` explains why the plugin is inactive, for example a missing `iata`.
- `MQTT connect refused: Not authorized` means the broker rejected the token. Make sure the system clock is correct (the token has `iat`/`exp` timestamps) and that a configured `private_key` belongs to this radio.
- `could not create auth token` means the radio couldn't sign the token. Update the companion firmware or set `private_key`.
- The `iata` value must exactly match the server's region code.
- The observer only appears after the radio has heard at least one packet. Set `runtime.log_level: DEBUG` to see each `MeshMapper: published packet …` line.
- No packets from other observers: check **MeshMapper live feed** in the web app's connections panel (it says why it's down) and that `subscribe` / `live_feed` aren't `false`. Quiet regions can go minutes without a packet. The **MQTT (subscribe)** connections stay off without a subscriber account, which is fine: observer logins can only publish. See [Receiving other observers' packets over MQTT](#receiving-other-observers-packets-over-mqtt).
- The observer doesn't show up in your LetsMesh or MeshRank account: for LetsMesh, set `owner_email` to your LetsMesh login email. For MeshRank, set `registration_key` (see [`letsmesh` and `meshrank`](#letsmesh-and-meshrank--more-mqtt-observers)).

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
│   ├── core_settings.py          # settings outside the plugins changed in the control panel (settings.json)
│   ├── config_export.py          # config.yaml with the control panel's changes
│   ├── yaml_round_trip.py        # editing YAML with its comments and layout kept
│   ├── plugin.py                 # BasePlugin, built-in plugin registry
│   ├── plugin_manager.py         # runs plugins; runtime on/off and settings (plugins.json)
│   ├── settings_schema.py        # JSON Schema subset: validation, secret masking
│   ├── radio_admin.py            # radio settings, channels and contacts
│   ├── firmware/                 # firmware updates: releases (catalog.py), file checks (images.py),
│   │                             # the update itself (updater.py), flashing process (worker.py)
│   ├── status.py                 # connection status registry
│   ├── text_utils.py
│   ├── types.py
│   ├── meshcore_packets.py       # raw MeshCore RF packet decoding
│   ├── transport/
│   │   └── meshcore.py           # MeshCoreTransport
│   ├── web/
│   │   ├── server.py             # HTTP server, auth/CSRF, SSE event hub
│   │   ├── api.py                # control panel JSON API
│   │   └── static/               # the page (index.html), control.js, check_in.js
│   └── plugins/
│       ├── bridge.py
│       ├── ping_pong.py
│       ├── trace_me.py
│       ├── dm_http_command.py
│       ├── mqtt_observer.py      # MQTT observer uploads/subscriptions shared by the four below
│       ├── meshmapper.py         # MeshMapper observer (+ its live feed)
│       ├── letsmesh.py           # LetsMesh observer (US/EU brokers)
│       ├── meshrank.py           # MeshRank observer (anonymous, registration key)
│       ├── custom_mqtt.py        # observer on MQTT servers of your choice
│       ├── packet_map.py         # map/messages data for the web app
│       ├── packet_map_store.py   # packet map SQLite persistence
│       ├── check_in.py           # daily/weekly check-ins: detection, replies, management
│       ├── check_in_api.py       # its routes: leaderboard API, control panel
│       ├── check_in_scoring.py   # schedules (days, weeks), streaks, ranks
│       └── check_in_store.py     # check-in SQLite persistence
├── leaderboard/                  # public check-in leaderboard site (deployed separately)
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
