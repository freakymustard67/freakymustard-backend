# freaky-backup

Standalone **backup streaming backend** for [streamda]'s "freaky mustard" (HF Space
`freakymustard67/Potato`). Instead of scraping VidSrc directly, it aggregates
**public Stremio addons** and exposes both:

- a **Stremio-addon-compatible API** (`/manifest.json`, `/stream/{type}/{id}.json`) — installable in Stremio directly, and
- a **frontend-friendly JSON API** (`/api/streams?type=movie&id=tt…`) with per-upstream diagnostics.

Two fully independent instances ship in one repo (separate processes/ports/configs):

| Instance | Port | Upstreams | Best for |
|---|---|---|---|
| **English** | `8101` | HDHub · DesiFlix · Torrentio | Hollywood/English movies & series, direct MP4/R2 links |
| **Tamil** | `8102` | Torrentio (TamilBlasters/TamilMV/Indian trackers) · DesiFlix · MediaFusion | Tamil & Indian content incl. dual-audio Hindi+Tamil |

## Quick start

```bash
cd freaky-backup
npm install            # only dep: webtorrent (for the direct-stream engine)
node server.js --config config/english.json   # :8101
node server.js --config config/tamil.json     # :8102
# or: ./start.sh   /   ./stop.sh
```

Open `http://127.0.0.1:8101/` or `/8102/` — landing page has a one-click
**Install in Stremio** button (`stremio://host:port/manifest.json`).

## Endpoints (per instance)

| Route | Purpose |
|---|---|
| `GET /manifest.json` | Stremio addon manifest |
| `GET /stream/movie/tt15239678.json` | movie streams (Stremio shape) |
| `GET /stream/series/tt0903747:1:1.json` | episode streams |
| `GET /api/streams?type=movie&id=tt15239678` | same + `{sources:[…]}` diagnostics (`&debug=1` also works on `/stream/*`) |
| `GET /healthz` · `GET /stats` | upstream health · counters/cache/engine stats |
| `GET /d/{infoHash}/{fileIdx}/{name}` | torrent→direct HTTP stream (engine), full Range support |

## Torrent → direct stream engine

Torrent results additionally get an **⚡direct** HTTP variant served by the built-in
webtorrent engine — browsers/players download the torrent server-side and receive
plain progressive bytes (206 Partial Content). Safety rails:

- Only infoHashes recently returned by aggregation are servable (no open relay).
- LRU cap (`maxTorrents`, default 3) + idle eviction (15 min) per instance.
- Metadata timeout 45 s → clean `502 {error:"torrent unavailable"}` if no peers.
- Client aborts mid-stream are handled (read streams destroyed, no leaks).

> Engine needs BitTorrent egress (verified working: ~3 MB/s from 16 peers).
> Set `publicBaseUrl` in config when behind an HTTPS reverse proxy so returned
> URLs use the right scheme/host.

## Reliability design

- Zero-dependency core (Node ≥18 native fetch/http); webtorrent lazy-loads only if installed.
- Per-request fan-out to all upstreams in parallel with hard timeouts + **one retry** each.
- **Response deadline** (18 s): clients never wait longer; late upstream results are folded into the cache asynchronously.
- TTL cache (10 min); empty results cached only 45 s so blips never blank a title long.
- Strict id validation (`tt\d+(:s:e)?`, `tmdb:`, `dsf:`) — path traversal/query injection rejected (400).
- SSRF guards: http(s)-only upstream/stream URLs.
- Process-level `uncaughtException`/`unhandledRejection` guards — a bad upstream payload can't kill the server.

## Config

Each file in `config/` is one instance: ports, branding/accent colors, cache TTL,
upstream timeout, deadline, engine toggles, and the upstream addon list (any
Stremio-compatible base URL works, including Torrentio config-path prefixes — the
Tamil instance uses `providers=…,tamilblasters,tamilmv,…`).

## Docker

```bash
docker compose up -d        # english on 8101, tamil on 8102
```

## Verified test matrix (`npm test` — 23 checks)

manifests/landing/health ×2 · EN movie Dune2 (**284 streams, 176 direct**) · upstream byte-probe (HTTP 206 from CDN) · **torrent→direct engine E2E (HTTP 206)** · EN series Breaking Bad S01E01 (127) · Tamil movie Vikram (16, dual-audio Hindi+Tamil sample verified) · Tamil series Mirzapur S01E01 (12 @ 0.7 s cold) · DesiFlix IMDb resolution (Shawshank 139–141) · traversal/garbage-id/404 hardening.

## Not included (by design)

No VidSrc re-scraper (that's what freaky mustard already does), no catalogs
(Torrentio-style stream-only addon), no debrid integration. streamda itself was
not touched — this project is fully standalone.

---

*Streams resolve from public third-party Stremio addons; availability of any
given title depends on those upstreams.*
