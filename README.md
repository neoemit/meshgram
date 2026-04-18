# Meshgram 🌐

Meshgram is a plugin-based bridge between **Meshtastic** and **Telegram**.

It lets you relay messages between a Telegram group and a Meshtastic channel, run channel-scoped automation plugins, and support direct-message command workflows.

## Highlights ✨

- 🔁 Bidirectional bridge between Telegram and Meshtastic
- 🧵 Cross-platform reply linking (Telegram replies ↔ Meshtastic replies)
- ❤️ Bidirectional emoji reaction sync for linked messages
- ✂️ UTF-8 byte-aware chunking for long Telegram messages
- 🧩 Plugin architecture for feature extensions
- 🐳 Docker support for Linux and macOS workflows
- 🛠️ Linux `systemd` deployment guide and templates

---

## Architecture 🧱

### Core runtime

- Loads secrets from `.env` and behavior config from `config.yaml`
- Connects to Telegram and Meshtastic transports
- Normalizes inbound events
- Dispatches events to enabled plugins
- Executes plugin actions with centralized logging/error handling

### Built-in plugins

- `bridge`
- `ping_pong`
- `dm_http_command`

---

## Project Layout 📁

```text
meshgram/
├── main.py
├── config.yaml
├── .env.example
├── Dockerfile
├── docker-compose.yml
├── docker-compose.macos-tcp.yml
├── docker-compose.linux-serial.yml
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
│   └── plugins/
│       ├── bridge.py
│       ├── ping_pong.py
│       └── dm_http_command.py
└── tests/
```

---

## Requirements ✅

- Python `3.12+`
- Telegram bot token
- Telegram group/chat ID
- Meshtastic node reachable over:
  - serial (`/dev/ttyUSB*`, `/dev/ttyACM*`, `/dev/tty.*`), or
  - TCP (`host:4403`)

---

## Environment Variables (`.env`) 🔐

Create your env file:

```bash
cp .env.example .env
```

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | none | Telegram bot token |
| `TELEGRAM_GROUP_ID` | Yes | none | Telegram target chat/group ID |
| `MESHGRAM_CONFIG_PATH` | No | `config.yaml` | YAML config path |
| `LOG_LEVEL` | No | `INFO` | Python logging level |
| `MESH_MODE` | No | from YAML (`serial`) | Meshtastic mode override (`serial`/`tcp`) |
| `MESH_NO_NODES` | No | from YAML (`false`) | Skip full node DB download on connect |
| `MESH_DEVICE` | No | from YAML | Serial device override |
| `MESH_HOST` | No | from YAML | TCP host override |
| `MESH_PORT` | No | from YAML or `4403` | TCP port override |
| `SOLAR_HOST` | No | none | Example env var for `dm_http_command` URL templating (`${SOLAR_HOST}`) |
| `SOLAR_TOKEN` | No | none | Example bearer token env var for `dm_http_command` auth |
| `SOLAR_API_KEY` | No | none | Example API key env var for `dm_http_command` headers |

---

## Configuration (`config.yaml`) ⚙️

### Complete example

```yaml
runtime:
  log_level: INFO

meshtastic:
  bridge_channel: 1
  node_name_overrides:
    "!1234abcd": "RTR-A"
  connection:
    mode: tcp
    no_nodes: true
    tcp_host: host.docker.internal
    tcp_port: 4403

telegram:
  include_captions: true
  sender_prefix_template: "[{display_name}] {message}"

chunking:
  enabled: true
  prefix_template: "({index}/{total}) "
  inter_chunk_delay_ms: 150
  payload_safety_margin_bytes: 24
  retry_max_attempts: 3
  retry_initial_delay_ms: 500
  retry_backoff_factor: 2.0
  abort_on_chunk_failure: true

plugins:
  - name: bridge
    enabled: true
    settings:
      channel: 1
      reply_link_ttl_hours: 24
      reactions_enabled: true
      meshtastic_want_ack: true
      missing_target_policy: fallback_message
      reply_missing_suffix: "(reply target not found)"
      reaction_missing_notice_template: "(reaction target not found)"

  - name: ping_pong
    enabled: true
    settings:
      keyword_responses:
        Ping: "Pong"
        Ack: "Ack"
      channels: [0, 1]

  - name: dm_http_command
    enabled: false
    settings:
      timeout_seconds: 8
      error_message: "Unable to fetch {command}"
      commands:
        BATTERY:
          url: "http://${SOLAR_HOST}/battery/"
          type: "json"
          value: "data.inv1.soc"
          msg: "{value}%"
          auth:
            type: bearer
            token_env: SOLAR_TOKEN
          headers:
            X-Api-Key: "${SOLAR_API_KEY}"
```

### Key config fields

#### `meshtastic`

- `bridge_channel`: default channel fallback for bridge plugin
- `node_name_overrides`: force stable sender labels if peer metadata is incomplete
  - key formats supported: `!1234abcd`, `1234abcd`, `305441741`, `0x1234abcd`
- `connection.mode`: `serial` or `tcp`
- `connection.no_nodes`: can improve resilience on proxied links, but may reduce dynamic node metadata

#### `telegram`

- `include_captions`: include media captions in Telegram → Meshtastic forwarding
- `sender_prefix_template`: supports `{display_name}` and `{message}` (default compact style is `[{display_name}] {message}`)

#### `chunking`

- Uses UTF-8 byte length (safe for emoji/multibyte text)
- `prefix_template` supports `{index}` and `{total}`
- `inter_chunk_delay_ms`: delay between chunk sends (default `150`)
- `payload_safety_margin_bytes`: reserves bytes below reported SDK payload max to reduce edge-size drops (default `24`)
- `retry_max_attempts`: retries per chunk before terminal failure (default `3`)
- `retry_initial_delay_ms`: delay before first retry (default `500`)
- `retry_backoff_factor`: exponential retry multiplier (default `2.0`)
- `abort_on_chunk_failure`: if `true`, stops remaining chunks in the same sequence after terminal failure

---

## Plugin Reference 🧩

### `bridge`

#### Meshtastic → Telegram

- forwards `TEXT_MESSAGE_APP` payloads
- channel filter: `bridge.settings.channel` (fallback `meshtastic.bridge_channel`)
- local-node loop suppression
- sender label order:
  1. `node_name_overrides`
  2. node `shortName`
  3. node `longName`
  4. normalized node ID
- if mapping exists, Meshtastic replies are forwarded as Telegram replies

#### Telegram → Meshtastic

- forwards Telegram `text`
- forwards media `caption` if enabled
- ignores bot-authored Telegram messages
- compacts Telegram sender names to first token (`Name Surname` → `Name`) before applying `sender_prefix_template`
- chunks oversized messages by UTF-8 bytes
- retries failed chunk sends with exponential backoff using `chunking` retry settings
- requires a packet ID confirmation from Meshtastic SDK responses for bridge sends (retries if missing)
- on terminal chunk failure, aborts later chunks in the same sequence when `abort_on_chunk_failure=true`
- chunk delivery failures are log-only (no Telegram failure notification)
- if mapping exists, Telegram replies are sent with Meshtastic `replyId`
- if reply target mapping is missing, message is still forwarded with `reply_missing_suffix`

#### Reply-link behavior

- in-memory mapping TTL set by `reply_link_ttl_hours` (default `24`)
- for chunked Telegram → Meshtastic sends:
  - first chunk is canonical for Telegram-reply lookup
  - replies to any chunk still map back to source Telegram message
- compatibility fallback for older Meshtastic SDKs:
  - `sendText(..., replyId=...)`
  - then `sendData(..., replyId=...)`
  - then low-level packet send with `decoded.reply_id`

#### Reaction behavior

- reaction sync is enabled with `reactions_enabled: true`
- scope is **linked messages only** (requires known cross-platform mapping)
- Telegram → Meshtastic:
  - only non-bot actors in configured group are considered
  - only first Unicode emoji reaction is mirrored
  - reaction removals and custom Telegram emoji are ignored
  - if Telegram only emits anonymous reaction-count updates, Meshgram uses best-effort emoji inference from count deltas
- Meshtastic → Telegram:
  - packets with `decoded.emoji` + `replyId/reply_id` are mapped as reactions
  - local-node packets are ignored for loop prevention
  - if Telegram rejects an emoji as `Reaction_invalid`, Meshgram retries with normalized variants, then safe fallback `👍`
  - if all candidates are rejected by Telegram, reaction sync is skipped (logged) without crashing plugin flow
- if reaction target mapping is missing and `missing_target_policy: fallback_message`, bridge emits `reaction_missing_notice_template`
- actor identity is platform-limited:
  - Telegram reaction appears from bot
  - Meshtastic reaction appears from bridge node

Bridge reaction/reply settings:

- `reactions_enabled`: enable/disable bidirectional reaction sync (`true` default)
- `meshtastic_want_ack`: request Meshtastic reliable delivery for bridge-originated outbound packets (`true` default)
- `missing_target_policy`: currently `fallback_message` (emit notice when mapping is missing)
- `reply_missing_suffix`: inline suffix appended when reply target mapping is missing
- `reaction_missing_notice_template`: notice sent when reaction target mapping is missing

### `ping_pong`

- listens on all channels by default, or `settings.channels` allowlist
- exact keyword matching using normalized, case-insensitive single-word input
- message edge punctuation/symbols are ignored (example: `ping?` matches `Ping`)
- command map is `settings.keyword_responses`
- replies on the **same incoming channel** and uses `replyId`

Example:

```yaml
- name: ping_pong
  enabled: true
  settings:
    keyword_responses:
      Ping: "Pong"
      Ack: "Ack"
    channels: [0, 1]
```

### `dm_http_command`

Responds to mapped **single-word direct messages** sent to your bridge node.

How it works:

1. A node sends a DM command (example: `BATTERY`)
2. Plugin matches command case-insensitively
3. Plugin performs HTTP GET to configured URL
4. Plugin parses response (`json` or `text`)
5. Plugin resolves configured `value` path
6. Plugin formats reply using `msg` template
7. Plugin replies to the sender as DM

Example mapping:

```yaml
- name: dm_http_command
  enabled: true
  settings:
    timeout_seconds: 8
    error_message: "Unable to fetch {command}"
    commands:
      BATTERY:
        url: "http://${SOLAR_HOST}/battery/"
        type: "json"
        value: "data.inv1.soc"
        msg: "{value}%"
        auth:
          type: bearer
          token_env: SOLAR_TOKEN
        headers:
          X-Api-Key: "${SOLAR_API_KEY}"
```

Given payload:

```json
{"data":{"inv1":{"name":"asd","soc":99}}}
```

`BATTERY` reply becomes:

```text
99%
```

Command settings:

- `url`: HTTP endpoint to call
  - supports env templating with `${ENV_VAR}`
- `type`: `json` or `text`
- `value`: extraction path (`.` separated; supports list indices for JSON arrays)
- `msg`: output template (supports `{value}` and `{command}`)
- `timeout_seconds`: optional per-command timeout (falls back to plugin-level timeout)
- `headers`: optional HTTP headers map (header values support `${ENV_VAR}`)
- `auth`: optional auth settings
  - `type`: currently `bearer`
  - `token_env`: env var containing token
  - `header`: optional header name override (default `Authorization`)
  - `prefix`: optional scheme prefix (default `Bearer`)

---

## Ways to Run Meshgram ▶️

### 1) Local Python (best for debugging) 🛠️

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env and config.yaml
python main.py
```

### 2) Docker Compose (base) 🐳

```bash
cp .env.example .env
# edit .env and config.yaml
docker compose up --build -d
docker compose logs -f meshgram
```

### 3) Docker + Linux USB serial passthrough 🐧

```bash
docker compose -f docker-compose.yml -f docker-compose.linux-serial.yml up --build -d
docker compose -f docker-compose.yml -f docker-compose.linux-serial.yml logs -f meshgram
```

Linux serial notes:

- set `MESH_DEVICE` in `.env` (example: `/dev/ttyUSB0`)
- verify device exists (`ls /dev/ttyUSB* /dev/ttyACM*`)
- ensure serial permissions/group access (`dialout` on many distros)

### 4) Docker on macOS with host serial→TCP bridge 🍎

Docker Desktop cannot directly pass macOS `/dev/tty.*` devices into Linux containers.

Install `socat`:

```bash
brew install socat
```

Start host serial bridge:

```bash
socat -d -d TCP-LISTEN:4403,reuseaddr,fork FILE:/dev/tty.usbmodem34B7DA5AFD281,raw,echo=0,b115200
```

Run Meshgram with macOS TCP override:

```bash
docker compose -f docker-compose.yml -f docker-compose.macos-tcp.yml up --build -d
docker compose -f docker-compose.yml -f docker-compose.macos-tcp.yml logs -f meshgram
```

---

## Linux `systemd` Service Setup 🧰

Use this when running Meshgram directly on Linux host Python (without Docker).

### 1) Create service user

```bash
sudo useradd --system --home /opt/meshgram --create-home --shell /usr/sbin/nologin meshgram
```

### 2) Install app files

```bash
sudo mkdir -p /opt/meshgram
sudo chown -R meshgram:meshgram /opt/meshgram
# copy repository files into /opt/meshgram (or clone there)
```

### 3) Create venv + install dependencies

```bash
sudo -u meshgram bash -lc 'cd /opt/meshgram && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt'
```

### 4) Configure environment and YAML

```bash
sudo -u meshgram cp /opt/meshgram/deploy/systemd/meshgram.env.example /opt/meshgram/.env
sudo -u meshgram nano /opt/meshgram/.env
sudo -u meshgram nano /opt/meshgram/config.yaml
```

### 5) Install provided unit file

```bash
sudo cp /opt/meshgram/deploy/systemd/meshgram.service /etc/systemd/system/meshgram.service
```

If your install path is not `/opt/meshgram`, update these fields in the unit file:

- `WorkingDirectory`
- `EnvironmentFile`
- `ExecStart`
- `ReadWritePaths`

### 6) Enable and start service

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now meshgram
sudo systemctl status meshgram --no-pager
```

### 7) Logs, restart, stop

```bash
journalctl -u meshgram -f
sudo systemctl restart meshgram
sudo systemctl stop meshgram
sudo systemctl disable meshgram
```

### 8) Serial permission tip

If serial access fails in serial mode:

```bash
sudo usermod -aG dialout meshgram
sudo systemctl restart meshgram
```

---

## Best-Practice Guidelines 📘

- Keep secrets in `.env`, not `config.yaml`
- Keep plugin scope narrow by channel and purpose
- Use `node_name_overrides` for deterministic sender labels
- Keep `dm_http_command` endpoints on trusted/internal networks
- Prefer short, unambiguous single-word command keys for DM command plugin
- Keep bot token used by only one active polling instance

---

## Testing 🧪

Run tests:

```bash
.venv/bin/python -m unittest discover -s tests
```

Current test coverage includes:

- config/env loading and override precedence
- chunking behavior (ASCII, emoji, long-token fallback)
- bridge filtering/loop prevention/reply mapping
- Telegram reaction parsing (`message_reaction` + anonymous count update fallback)
- Meshtastic reaction parsing compatibility (portnum/emoji format variants)
- ping keyword-response behavior and channel filtering
- DM HTTP command plugin behavior
- sender name resolution fallback/override behavior

---

## Troubleshooting 🩺

### Meshtastic connection fails

- verify selected mode (`serial` or `tcp`)
- verify host/device values in env + YAML
- on macOS Docker, ensure `socat` is listening on `4403`
- verify container can reach `host.docker.internal`

### Telegram `409 Conflict` on polling

- only one bot polling process can run per token
- stop duplicate local/container instance

### Messages are not bridging

- verify `TELEGRAM_GROUP_ID`
- verify bridge channel (`bridge.settings.channel`)
- ensure source message has textual content (`text` or enabled `caption`)
- ensure Telegram sender is not a bot

### Telegram long message arrives partially on Meshtastic

- ensure chunking is enabled (`chunking.enabled: true`)
- increase `chunking.inter_chunk_delay_ms` (for busy links)
- increase `chunking.payload_safety_margin_bytes` if you still see first-chunk drops
- keep `bridge.settings.meshtastic_want_ack: true`
- watch logs for `Meshtastic send exhausted retries` to identify failing chunk indexes

### Reactions are not syncing

- confirm `bridge.settings.reactions_enabled: true`
- test on recently bridged/linked messages (reaction sync requires mapping)
- for Telegram → Meshtastic, ensure bot can receive reaction updates in that group
- if you see only anonymous reaction counts from Telegram, keep the bot running long enough to establish per-message baseline deltas

### Sender label shows node ID

- expected when peer metadata is unavailable
- set `meshtastic.node_name_overrides` for deterministic labels
- disable `no_nodes` if you prefer richer dynamic node metadata
