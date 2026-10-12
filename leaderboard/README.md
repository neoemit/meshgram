# 🏅 MeshCore check-in leaderboard

A public web page for the weekly mesh check-in (`#MeshCoreMonday`): who's on the air this week, in order, the longest running streaks, and everyone's standing. It shows what Meshgram's [`check_in` plugin](../README.md#check_in--weekly-check-ins-with-streaks-and-a-leaderboard) records and is deployed **separately from Meshgram**, on any machine that can reach Meshgram's web app.

- **One small container:** nginx (unprivileged, read-only filesystem, no capabilities) serves the page and proxies the leaderboard from Meshgram. Nothing is stored here.
- **The token stays server-side.** Browsers only talk to this site; nginx adds `Authorization: Bearer <token>` when it asks Meshgram (`GET /api/plugins/check_in/leaderboard`). No CORS, no credentials in the page.
- **Meshgram is shielded.** One answer is shared by every visitor for `CACHE_SECONDS` (concurrent requests wait for one fetch), so traffic to this site never turns into traffic to the radio host. While Meshgram can't be reached, the last leaderboard is served; the page says "Signal lost" and keeps retrying.
- **The page:** plain HTML/CSS/JS, no build step and no third-party requests (strict CSP). Live "on air" lamp and countdown to the next check-in, stats, a podium, this week's roll call, a searchable leaderboard, new check-ins flashing in, light and dark themes, phone layout, and an animated mesh in the background (still with reduced motion).

## Set up

1. **In Meshgram** (control panel → Plugins → *Weekly check-ins*): turn the plugin on and set **Leaderboard API token** to a long random string, e.g. `openssl rand -hex 32`. Check it from the leaderboard's host:

   ```bash
   curl -H "Authorization: Bearer $TOKEN" http://<meshgram-host>:8080/api/plugins/check_in/leaderboard
   ```

   Meshgram's web app must be reachable from this host: in Docker it listens on `0.0.0.0` (`web.host`), port 8080 by default. Without a web password the leaderboard needs only the token; other API routes are unaffected by it.

2. **Here:**

   ```bash
   cp .env.example .env     # set MESHGRAM_URL and MESHGRAM_API_TOKEN
   docker compose up -d
   curl -s http://localhost:8090/api/leaderboard | head -c 300
   ```

   Open `http://<host>:8090/`.

3. **Publish it** behind a TLS reverse proxy pointing at port `8090` (Caddy: `leaderboard.example.org { reverse_proxy localhost:8090 }`; or Traefik, nginx, a Cloudflare tunnel, …). If the proxy runs in Docker on the same host, you can attach this service to its network instead of publishing the port.

## Settings (`.env`)

| Variable | Default | |
|---|---|---|
| `MESHGRAM_URL` | — (required) | Meshgram's web app as this host reaches it, e.g. `http://192.168.1.20:8080` or `https://meshgram.example.org` |
| `MESHGRAM_API_TOKEN` | — (required) | The `check_in` plugin's `api_token` (letters and digits) |
| `PORT` | `8090` | Host port of the site |
| `REFRESH_SECONDS` | `30` | How often open pages poll for news |
| `CACHE_SECONDS` | `15` | How long one answer from Meshgram is shared |
| `MESHGRAM_TLS_VERIFY` | `on` | `off` accepts a self-signed certificate on an `https://` URL |
| `RESOLVER` | `127.0.0.11` | DNS server for `MESHGRAM_URL`'s host name (Docker's own resolves public names too) |

After changing `.env`: `docker compose up -d` (it recreates the container).

## Layout

```text
leaderboard/
├── docker-compose.yml
├── .env.example
├── nginx/default.conf.template   # site + proxy/cache; rendered from the environment at start
└── site/                         # the page: index.html, styles.css, app.js, favicon.svg
```

The page reads `api/leaderboard` (this site's proxy) and `config.json` (the refresh interval, from nginx). To work on it without Meshgram, serve `site/` with anything that also answers those two paths, for example a few lines of Python returning a saved leaderboard JSON.

## What the leaderboard contains

`GET /api/plugins/check_in/leaderboard` (proxied as `/api/leaderboard`) returns:

```json
{
  "title": "MeshCore Monday",
  "keyword": "#MeshCoreMonday",
  "timezone": "Europe/Rome",
  "check_in_days": ["Monday"],
  "week": "2026-10-12",
  "window": {"open": true, "opens_at": "2026-10-11T22:00:00Z", "closes_at": "2026-10-12T22:00:00Z"},
  "participants_this_week": 13,
  "generated_at": "2026-10-12T09:30:00Z",
  "meshgram_title": "LMB Meshgram",
  "leaderboard": [
    {"rank": 1, "id": "A1B2…", "hash": "A1", "name": "LMB-T", "streak": 15, "best_streak": 20,
     "total_check_ins": 40, "last_check_in": "2026-10-12T07:02:11Z", "checked_in_this_week": true,
     "week_rank": 13, "resolved": true}
  ]
}
```

`meshgram_title` is Meshgram's `web.title` (control panel → Settings → Web app → Title): the page's top bar and footer credit it ("live from the mesh via LMB Meshgram"). Sorted by streak, longest first (`rank` is shared by equal streaks). `id` is the node's public key and `hash` its first byte; both are `null` (and `resolved` false) for a node known by name only, until Meshgram hears its advert. Individual check-ins are never exposed.

## Troubleshooting

- **"No signal" on the page / `502` from `/api/leaderboard`:** this host can't reach `MESHGRAM_URL`. Check from inside the container: `docker compose exec leaderboard sh -c 'wget -S -O /dev/null "$MESHGRAM_URL/healthz"'` (any HTTP status, even `401`, means Meshgram is reachable).
- **`401`:** the token doesn't match the plugin's `api_token`. **`404`:** the `check_in` plugin isn't running.
- **Nothing starts:** `docker compose logs leaderboard` shows nginx's complaint (a missing variable in `.env`, a token with quotes in it, …).
