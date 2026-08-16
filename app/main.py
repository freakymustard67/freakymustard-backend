"""FreakyMustard Proxy — ad-free stream resolution for FreakyMustard.

Resolves clean HLS streams from the VidSrc (vidsrcme) API and relays every
byte through a signed, stateless HLS proxy so the browser never loads the
provider's ad-laden page.

Why a byte proxy at all?
  The CDN sits behind a Cloudflare WAF that returns 403 for any request that
  carries an ``Origin`` or ``Referer`` header, but allows bare requests. Real
  browsers ALWAYS send ``Origin`` on cross-origin fetches, so the frontend can
  never play the CDN directly — the proxy (which sends neither header) must
  relay master playlists, variant playlists and media segments.

Endpoints
---------
GET /health
GET /health/deep   — end-to-end chain check (resolve->token->master->variant->segment)
GET /resolve/movie/{imdb_id}
GET /resolve/tv/{imdb_id}/{season}/{episode}
GET /hls/{token}   — HLS master/variant playlists (URL-rewritten) and media
                     segments (streamed). Range supported for segments.

Every /hls url is HMAC-signed and embeds the exact upstream url, so this
service can never be abused as an open proxy.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from cache import TTLCache
from signing import sign_url, verify_token
from vidsrc import StreamResult, VidSrcError, resolve_movie, resolve_tv, UA

VERSION = "2.0.0"
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*")
RESOLVE_TTL = int(os.environ.get("RESOLVE_TTL", "240"))  # master-url resolve cache
HLS_TOKEN_TTL = int(os.environ.get("HLS_TOKEN_TTL", "21600"))  # signed url validity (6h)
UPSTREAM_TOKEN_TTL = int(os.environ.get("UPSTREAM_TOKEN_TTL", "180"))  # per-host token cache
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

_rl: dict[str, list[float]] = {}
RL_LIMIT = int(os.environ.get("RL_LIMIT", "60"))
RL_WINDOW = 60.0

_IMDB_RE = re.compile(r"^tt\d{4,10}$")

# The CDN WAF 403s any request with Origin/Referer. Send NEITHER. A browser
# User-Agent alone is enough.
_CDN_HEADERS = {
    "User-Agent": UA,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

_client: httpx.AsyncClient | None = None
_upstream_tokens: dict[str, tuple[float, str]] = {}  # host origin -> (mono expiry, token)
_token_lock = asyncio.Lock()


async def cdn() -> httpx.AsyncClient:
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


# --- upstream token (IP-bound, per host) -----------------------------------


async def _upstream_token(host_origin: str) -> str:
    now = time.monotonic()
    hit = _upstream_tokens.get(host_origin)
    if hit and hit[0] > now:
        return hit[1]
    async with _token_lock:
        hit = _upstream_tokens.get(host_origin)
        if hit and hit[0] > now:
            return hit[1]
        client = await cdn()
        try:
            resp = await client.get(f"{host_origin}/generate.php", headers=_CDN_HEADERS)
            token = resp.text.strip()
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"token fetch failed: {exc}")
        if not token:
            raise HTTPException(502, "empty upstream token")
        _upstream_tokens[host_origin] = (now + UPSTREAM_TOKEN_TTL, token)
        return token


# --- url helpers ------------------------------------------------------------


def _strip_token(url: str) -> str:
    """Remove any ``token`` query param so we can stamp a fresh one."""
    p = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k != "token"]
    return urlunparse(p._replace(query=urlencode(q)))


def _stamp_token(url: str, token: str) -> str:
    p = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k != "token"]
    q.append(("token", token))
    return urlunparse(p._replace(query=urlencode(q)))


def _is_playlist(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".m3u8")


def _rewrite_playlist(text: str, base_upstream_url: str, base: str) -> str:
    """Rewrite every URL in an HLS playlist to route back through /hls."""
    out = []
    uri_re = re.compile(r'URI="([^"]+)"')
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line)
            continue
        if stripped.startswith("#"):
            def _repl(m: re.Match) -> str:
                resolved = _strip_token(urljoin(base_upstream_url, m.group(1)))
                return f'URI="{base}/hls/{sign_url(resolved, ttl=HLS_TOKEN_TTL)}"'

            out.append(uri_re.sub(_repl, line))
        else:
            resolved = _strip_token(urljoin(base_upstream_url, stripped))
            out.append(f"{base}/hls/{sign_url(resolved, ttl=HLS_TOKEN_TTL)}")
    return "\n".join(out) + "\n"


# --- serialization ----------------------------------------------------------


def _serialize(result: StreamResult, request: Request, kind: str) -> dict:
    base = _public_base(request)
    sources = []
    for i, master in enumerate(result.masters):
        token = sign_url(_strip_token(master), ttl=HLS_TOKEN_TTL)
        sources.append(
            {
                "label": f"Server {i + 1}",
                "url": f"{base}/hls/{token}",
                "format": "hls",
                "direct": False,
            }
        )
    return {
        "provider": result.provider,
        "kind": kind,
        "mediaType": result.media_type,
        "sourceId": result.source_id,
        "title": result.title,
        "imdbId": result.imdb_id,
        "ttl": result.ttl,
        "sources": sources,
        "captions": [],
    }


async def _resolve_cached(key: str, fetch) -> StreamResult:
    loop = asyncio.get_running_loop()
    return await _cache.get_or_fetch(
        key, lambda: loop.run_in_executor(None, fetch), ttl=RESOLVE_TTL
    )


# --- routes -----------------------------------------------------------------


@app.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "version": VERSION,
        "providers": ["vidsrc"],
        "cache": _cache.stats(),
        "time": int(time.time()),
    }


@app.get("/", response_class=HTMLResponse)
async def root() -> str:
    return (
        "<!doctype html><meta charset=utf-8><title>FreakyMustard Proxy</title>"
        "<body style='background:#0a0a0c;color:#e2e8f0;font-family:monospace;"
        "display:grid;place-items:center;height:100vh;margin:0'>"
        "<div><h1>&#127916; FreakyMustard Proxy</h1>"
        "<p>Ad-free HLS stream resolution service.</p>"
        "<p><a style='color:#3b82f6' href='/health'>/health</a></p></div>"
    )


@app.get("/health/deep")
async def health_deep() -> JSONResponse:
    """End-to-end chain check: resolve -> decrypt -> token -> master -> segment.

    Unlike /health (which only proves the process is up), this proves the
    whole upstream chain still works. Intended for a watchdog cron. Kept
    cheap: one resolve (cached), one master fetch, one ~1-byte segment probe.
    """
    t0 = time.monotonic()
    report: dict = {"ok": False, "stages": {}, "ms": 0}

    def stage(name: str, ok: bool, detail: str = "") -> None:
        report["stages"][name] = {"ok": ok, "detail": detail}

    try:
        # 1. Resolve a well-known, stable title (Inception).
        try:
            result = await _resolve_cached("movie:tt1375666", lambda: resolve_movie("tt1375666"))
            stage("resolve", bool(result.masters), f"{len(result.masters)} masters")
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            stage("resolve", False, str(exc)[:200])
            report["ms"] = int((time.monotonic() - t0) * 1000)
            return JSONResponse(report, status_code=200)

        master = _strip_token(result.masters[0])
        parsed = urlparse(master)
        host_origin = f"{parsed.scheme}://{parsed.netloc}"

        # 2. Upstream token (IP-bound).
        try:
            up_token = await _upstream_token(host_origin)
            stage("token", bool(up_token), f"{len(up_token)} chars")
        except Exception as exc:  # noqa: BLE001
            stage("token", False, str(exc)[:200])
            report["ms"] = int((time.monotonic() - t0) * 1000)
            return JSONResponse(report, status_code=200)

        client = await cdn()

        # 3. Master playlist.
        try:
            resp = await client.get(_stamp_token(master, up_token), headers=_CDN_HEADERS)
            ok = resp.status_code == 200 and "#EXTM3U" in resp.text
            stage("master", ok, f"http {resp.status_code}")
            if not ok:
                report["ms"] = int((time.monotonic() - t0) * 1000)
                return JSONResponse(report, status_code=200)
        except Exception as exc:  # noqa: BLE001
            stage("master", False, str(exc)[:200])
            report["ms"] = int((time.monotonic() - t0) * 1000)
            return JSONResponse(report, status_code=200)

        # 4. Variant playlist (master lists variant playlists, not segments).
        variant_url = None
        for line in resp.text.splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                variant_url = urljoin(master, s)
                break
        if not variant_url:
            stage("variant", False, "no variant in master")
            report["ms"] = int((time.monotonic() - t0) * 1000)
            return JSONResponse(report, status_code=200)
        try:
            var_resp = await client.get(
                _stamp_token(_strip_token(variant_url), up_token), headers=_CDN_HEADERS
            )
            ok = var_resp.status_code == 200 and "#EXTM3U" in var_resp.text
            stage("variant", ok, f"http {var_resp.status_code}")
            if not ok:
                report["ms"] = int((time.monotonic() - t0) * 1000)
                return JSONResponse(report, status_code=200)
        except Exception as exc:  # noqa: BLE001
            stage("variant", False, str(exc)[:200])
            report["ms"] = int((time.monotonic() - t0) * 1000)
            return JSONResponse(report, status_code=200)

        # 5. First media segment (probe 1 byte — enough to confirm access).
        seg_url = None
        for line in var_resp.text.splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                seg_url = urljoin(variant_url, s)
                break
        if not seg_url:
            stage("segment", False, "no segment in variant")
            report["ms"] = int((time.monotonic() - t0) * 1000)
            return JSONResponse(report, status_code=200)
        try:
            seg_resp = await client.get(
                _stamp_token(_strip_token(seg_url), up_token),
                headers={**_CDN_HEADERS, "Range": "bytes=0-0"},
            )
            first = seg_resp.content[:1]
            ok = seg_resp.status_code in (200, 206) and first == b"\x47"
            stage("segment", ok, f"http {seg_resp.status_code} first_byte={first.hex() or 'empty'}")
        except Exception as exc:  # noqa: BLE001
            stage("segment", False, str(exc)[:200])

        report["ok"] = all(s["ok"] for s in report["stages"].values())
        report["ms"] = int((time.monotonic() - t0) * 1000)
        return JSONResponse(report, status_code=200)
    except Exception as exc:  # noqa: BLE001
        report["error"] = str(exc)[:200]
        report["ms"] = int((time.monotonic() - t0) * 1000)
        return JSONResponse(report, status_code=200)


@app.get("/resolve/movie/{imdb_id}")
async def resolve_movie_route(imdb_id: str, request: Request):
    if not _IMDB_RE.match(imdb_id):
        raise HTTPException(400, "imdb_id must look like tt1234567")
    if _rate_limited(request.client.host if request.client else "?"):
        raise HTTPException(429, "rate limited")
    try:
        result = await _resolve_cached(f"movie:{imdb_id}", lambda: resolve_movie(imdb_id))
    except VidSrcError as exc:
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
    except VidSrcError as exc:
        raise HTTPException(502, str(exc))
    return JSONResponse(_serialize(result, request, "tv"))


@app.get("/hls/{token}")
async def hls(token: str, request: Request):
    data = verify_token(token)
    if not data:
        raise HTTPException(403, "invalid or expired token")
    upstream_url = data["u"]

    parsed = urlparse(upstream_url)
    host_origin = f"{parsed.scheme}://{parsed.netloc}"
    up_token = await _upstream_token(host_origin)
    fetch_url = _stamp_token(upstream_url, up_token)

    client = await cdn()
    rng = request.headers.get("range")

    if _is_playlist(upstream_url):
        # Playlists are small: fetch fully, rewrite, return.
        try:
            resp = await client.get(fetch_url, headers=_CDN_HEADERS)
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"upstream error: {exc}")
        if resp.status_code >= 400:
            raise HTTPException(resp.status_code, f"upstream {resp.status_code}")
        base = _public_base(request)
        rewritten = _rewrite_playlist(resp.text, upstream_url, base)
        return Response(
            content=rewritten.encode("utf-8"),
            media_type="application/vnd.apple.mpegurl",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "no-cache",
            },
        )

    # Media segment: stream bytes, honour Range for seeking.
    headers = dict(_CDN_HEADERS)
    if rng:
        headers["Range"] = rng
    try:
        req = client.build_request("GET", fetch_url, headers=headers)
        resp = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"upstream error: {exc}")

    if resp.status_code >= 400:
        await resp.aclose()
        raise HTTPException(resp.status_code, f"upstream {resp.status_code}")

    passthrough = {}
    for h in ("content-length", "content-range", "accept-ranges"):
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

    # The CDN disguises segments as text/html; force the real media type so
    # players/MSE treat them correctly.
    upstream_ct = resp.headers.get("content-type", "")
    media_type = upstream_ct if upstream_ct.startswith(("video/", "audio/")) else "video/mp2t"

    return StreamingResponse(
        streamer(),
        status_code=resp.status_code,
        media_type=media_type,
        headers=passthrough,
    )


@app.head("/hls/{token}")
async def hls_head(token: str, request: Request):
    data = verify_token(token)
    if not data:
        raise HTTPException(403, "invalid or expired token")
    upstream_url = data["u"]
    parsed = urlparse(upstream_url)
    host_origin = f"{parsed.scheme}://{parsed.netloc}"
    up_token = await _upstream_token(host_origin)
    fetch_url = _stamp_token(upstream_url, up_token)
    client = await cdn()
    try:
        resp = await client.head(fetch_url, headers=_CDN_HEADERS)
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "7860")))
