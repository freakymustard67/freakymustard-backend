---
title: Streamda Proxy
emoji: 🎬
colorFrom: indigo
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
---

# Streamda Proxy

Ad-free HLS stream resolution service for [Streamda](https://github.com/Abhishek4512009/web-stream).

Resolves clean HLS streams from the VidSrc (vidsrcme) API and relays every byte
through a signed, stateless HLS proxy so the browser never loads the provider's
ad-laden page.

## Why a byte proxy?

The CDN sits behind a Cloudflare WAF that returns **403 for any request carrying
an `Origin` or `Referer` header**, but allows bare requests. Real browsers always
send `Origin` on cross-origin fetches, so the frontend can never play the CDN
directly — this proxy (which sends neither header) relays master playlists,
variant playlists and media segments.

## Endpoints

| Route | Description |
| --- | --- |
| `GET /health` | Liveness + cache stats |
| `GET /resolve/movie/{imdb_id}` | Resolve movie HLS sources (IMDB id, e.g. `tt1375666`) |
| `GET /resolve/tv/{imdb_id}/{s}/{e}` | Resolve TV episode HLS sources |
| `GET /hls/{token}` | HLS master/variant playlists (URL-rewritten) + media segments (streamed, Range supported) |
| `GET /download/{token}?filename=…` | Assemble a resolved stream into one MPEG-TS file (best variant, attachment download) |

Every `/hls` URL is HMAC-signed and embeds the exact upstream URL, so this
service cannot be abused as an open proxy.

## Providers

- **vidsrc** (vidsrcme) — encrypted API; `stream_urls` is a base64 ChaCha20 blob
  decrypted in-process via the provider's per-5-minute-window WASM (run with
  `wasmtime`). Accepts IMDB ids directly for both movies and TV.

## Environment

| Var | Default | Purpose |
| --- | --- | --- |
| `PROXY_SECRET` | (dev default) | HMAC secret for URL signing — **set in production** |
| `PUBLIC_BASE_URL` | auto | Base URL embedded in returned HLS links |
| `ALLOWED_ORIGINS` | `*` | CORS origins (comma-separated) |
| `RESOLVE_TTL` | `240` | Seconds to cache resolve results |
| `HLS_TOKEN_TTL` | `21600` | Signed `/hls` URL validity (seconds) |
| `UPSTREAM_TOKEN_TTL` | `180` | Per-host upstream token cache (seconds) |
| `RL_LIMIT` | `60` | Resolve requests per IP per 60s |

## Local dev

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cd app && uvicorn main:app --host 127.0.0.1 --port 7860
```
