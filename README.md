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

Ad-free stream resolution service for [Streamda](https://github.com/Abhishek4512009/web-stream).

Resolves direct media URLs from provider APIs and serves them through a signed,
Range-capable streaming proxy so the browser never loads the provider's page
(and therefore never sees its ads).

## Endpoints

| Route | Description |
| --- | --- |
| `GET /health` | Liveness + cache stats |
| `GET /resolve/movie/{imdb_id}` | Resolve movie sources (IMDB id, e.g. `tt1375666`) |
| `GET /resolve/tv/{imdb_id}/{s}/{e}` | Resolve TV episode sources |
| `GET /media/{token}` | Streaming media proxy (mp4/hls), Range/seek supported |
| `GET /caption/{token}` | Subtitle proxy, SRT auto-converted to WebVTT |

Media/caption URLs are HMAC-signed and expire with the upstream CDN token, so
this service cannot be abused as an open proxy.

## Providers

- **vidlink** — encrypted API (XSalsa20-Poly1305), accepts IMDB ids directly.

## Environment

| Var | Default | Purpose |
| --- | --- | --- |
| `PROXY_SECRET` | (dev default) | HMAC secret for URL signing — **set in production** |
| `PUBLIC_BASE_URL` | auto | Base URL embedded in returned media links |
| `ALLOWED_ORIGINS` | `*` | CORS origins (comma-separated) |
| `RESOLVE_TTL` | `1500` | Seconds to cache resolve results |
| `RL_LIMIT` | `30` | Resolve requests per IP per 60s |
