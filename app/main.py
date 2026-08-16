"""Streamda Proxy — ad-free stream resolution for Streamda.

Resolves direct media URLs from provider APIs (VidLink) and serves them
through a signed, Range-capable streaming proxy so the browser never
touches the provider's page (and therefore never sees its ads).

Endpoints
---------
GET /health
GET /resolve/movie/{imdb_id}
GET /resolve/tv/{imdb_id}/{season}/{episode}
GET /media/{token}      — streaming proxy (mp4/hls segments), Range supported
GET /caption/{token}    — subtitle proxy, SRT auto-converted to WebVTT

All media/caption urls are HMAC-signed and expire with the upstream CDN
token, so this service cannot be used as an open proxy.
"""

from __future__ import annotations

import asyncio
import os
import re
import time

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from cache import TTLCache
from signing import sign_url, verify_token
from vidlink import (
    CDN_ORIGIN,
    CDN_REFERER,
    StreamResult,
    VidLinkError,
    resolve_movie,
    resolve_tv,
)

VERSION = "1.0.0"
ALLOWED_ORIGINS = os.environ.get(
    "ALLOWED_ORIGINS", "*"
)  # comma-separated list or "*"
RESOLVE_TTL = int(os.environ.get("RESOLVE_TTL", "1500"))  # < CDN url ttl
CHUNK = 256 * 1024

app = FastAPI(title="Streamda Proxy", version=VERSION, docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if ALLOWED_ORIGINS == "*" else ALLOWED_ORIGINS.split(","),
    allow_credentials=False,
    allow_methods=["GET", "HEAD", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Content-Range", "Accept-Ranges", "Content-Length"],
)

_cache = TTLCache(default_ttl=RESOLVE_TTL, max_entries=1024)

# Simple per-IP resolve rate limit: N requests per window.
_rl: dict[str, list[float]] = {}
RL_LIMIT = int(os.environ.get("RL_LIMIT", "30"))
RL_WINDOW = 60.0

_IMDB_RE = re.compile(r"^tt\d{4,10}$")

_UPSTREAM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
    ),
    "Referer": CDN_REFERER,
    "Origin": CDN_ORIGIN,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "video",
    "Sec-Fetch-Mode": "no-cors",
    "Sec-Fetch-Site": "cross-site",
}

_client: httpx.AsyncClient | None = None


async def upstream() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            follow_redirects=True, timeout=httpx.Timeout(30.0, read=120.0)
        )
    return _client


@app.on_event("shutdown")
async def _close() -> None:
    if _client and not _client.is_closed:
        await _client.aclose()


def _rate_limited(ip: str) -> bool:
    now = time.monotonic()
    hits = [t for t in _rl.get(ip, []) if now - t < RL_WINDOW]
    hits.append(now)
    _rl[ip] = hits
    return len(hits) > RL_LIMIT


def _public_base(request: Request) -> str:
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if base:
        return base
    return str(request.base_url).rstrip("/")


def _serialize(result: StreamResult, request: Request, kind: str) -> dict:
    base = _public_base(request)
    sources = []
    for q in result.qualities:
        token = sign_url(q.url, ttl=result.ttl, referer=CDN_REFERER)
        sources.append(
            {
                "quality": q.label,
                "url": f"{base}/media/{token}",
                "direct": False,
                "format": q.fmt,
                "codec": q.codec,
                "size": q.size,
            }
        )
    captions = []
    for c in result.captions:
        token = sign_url(c.url, ttl=result.ttl, referer=CDN_REFERER)
        captions.append({"label": c.label, "url": f"{base}/caption/{token}"})
    return {
        "provider": result.provider,
        "kind": kind,
        "mediaType": result.media_type,
        "sourceId": result.source_id,
        "ttl": result.ttl,
        "sources": sources,
        "captions": captions,
    }


async def _resolve_cached(key: str, fetch) -> StreamResult:
    loop = asyncio.get_running_loop()
    return await _cache.get_or_fetch(
        key, lambda: loop.run_in_executor(None, fetch), ttl=RESOLVE_TTL
    )


@app.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "version": VERSION,
        "providers": ["vidlink"],
        "cache": _cache.stats(),
        "time": int(time.time()),
    }


@app.get("/", response_class=HTMLResponse)
async def root() -> str:
    return (
        "<!doctype html><meta charset=utf-8><title>Streamda Proxy</title>"
        "<body style='background:#0a0a0c;color:#e2e8f0;font-family:monospace;"
        "display:grid;place-items:center;height:100vh;margin:0'>"
        "<div><h1>&#127916; Streamda Proxy</h1>"
        "<p>Ad-free stream resolution service.</p>"
        "<p><a style='color:#3b82f6' href='/health'>/health</a></p></div>"
    )


@app.get("/resolve/movie/{imdb_id}")
async def resolve_movie_route(imdb_id: str, request: Request):
    if not _IMDB_RE.match(imdb_id):
        raise HTTPException(400, "imdb_id must look like tt1234567")
    if _rate_limited(request.client.host if request.client else "?"):
        raise HTTPException(429, "rate limited")
    try:
        result = await _resolve_cached(
            f"movie:{imdb_id}", lambda: resolve_movie(imdb_id)
        )
    except VidLinkError as exc:
        raise HTTPException(502, str(exc))
    return JSONResponse(_serialize(result, request, "movie"))


@app.get("/resolve/tv/{imdb_id}/{season}/{episode}")
async def resolve_tv_route(imdb_id: str, season: int, episode: int, request: Request):
    if not _IMDB_RE.match(imdb_id):
        raise HTTPException(400, "imdb_id must look like tt1234567")
    if not (0 <= season <= 99 and 0 <= episode <= 999):
        raise HTTPException(400, "season/episode out of range")
    if _rate_limited(request.client.host if request.client else "?"):
        raise HTTPException(429, "rate limited")
    try:
        result = await _resolve_cached(
            f"tv:{imdb_id}:{season}:{episode}",
            lambda: resolve_tv(imdb_id, season, episode),
        )
    except VidLinkError as exc:
        raise HTTPException(502, str(exc))
    return JSONResponse(_serialize(result, request, "tv"))


async def _proxy(token: str, request: Request, convert_vtt: bool):
    data = verify_token(token)
    if not data:
        raise HTTPException(403, "invalid or expired token")
    url = data["u"]

    headers = dict(_UPSTREAM_HEADERS)
    if data.get("r"):
        headers["Referer"] = data["r"]
    # Pass through Range for seeking.
    rng = request.headers.get("range")
    if rng:
        headers["Range"] = rng

    client = await upstream()
    try:
        req = client.build_request("GET", url, headers=headers)
        resp = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"upstream error: {exc}")

    if resp.status_code >= 400:
        body = (await resp.aread())[:200]
        await resp.aclose()
        raise HTTPException(resp.status_code, f"upstream {resp.status_code}: {body[:120]!r}")

    if convert_vtt:
        raw = await resp.aread()
        await resp.aclose()
        text = raw.decode("utf-8", errors="replace")
        if not text.lstrip().upper().startswith("WEBVTT"):
            # SRT -> VTT: fix timestamp commas, prepend header.
            text = re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", text)
            text = "WEBVTT\n\n" + text.strip() + "\n"
        return Response(
            content=text.encode("utf-8"),
            media_type="text/vtt; charset=utf-8",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "public, max-age=300",
            },
        )

    passthrough = {}
    for h in ("content-type", "content-length", "content-range", "accept-ranges"):
        if h in resp.headers:
            passthrough[h] = resp.headers[h]
    passthrough["Access-Control-Allow-Origin"] = "*"
    passthrough["Cache-Control"] = "public, max-age=300"

    async def streamer():
        try:
            async for chunk in resp.aiter_bytes(CHUNK):
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(
        streamer(),
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/octet-stream"),
        headers=passthrough,
    )


@app.get("/media/{token}")
async def media(token: str, request: Request):
    return await _proxy(token, request, convert_vtt=False)


@app.head("/media/{token}")
async def media_head(token: str, request: Request):
    data = verify_token(token)
    if not data:
        raise HTTPException(403, "invalid or expired token")
    client = await upstream()
    headers = dict(_UPSTREAM_HEADERS)
    if data.get("r"):
        headers["Referer"] = data["r"]
    try:
        resp = await client.head(data["u"], headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(502, str(exc))
    return Response(
        status_code=resp.status_code,
        headers={
            k: v
            for k, v in resp.headers.items()
            if k.lower() in ("content-type", "content-length", "accept-ranges")
        }
        | {"Access-Control-Allow-Origin": "*"},
    )


@app.get("/caption/{token}")
async def caption(token: str, request: Request):
    return await _proxy(token, request, convert_vtt=True)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "7860")))
