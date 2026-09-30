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
GET /download/{token}?filename=… — assemble a resolved HLS stream into one
                     MPEG-TS file and stream it as an attachment.
GET /api/english/… — Cinemeta catalogue, details, embed servers, torrents.
GET /api/years | /api/movies | /api/search | /api/details | /api/files |
     /api/stream | /api/auto-stream — Tamil scraper traversal + Mongo index.

Every /hls url is HMAC-signed and embeds the exact upstream url, so this
service can never be abused as an open proxy.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
import logging
import re
import time
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse, urlunparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from cache import TTLCache
from signing import sign_url, verify_token
from vidsrc import StreamResult, VidSrcError, resolve_movie, resolve_tv, UA
from english import router as english_router
from backup import router as backup_router
from indexer import MovieIndexer, canonical_link
from scraper import MoviesdaScraper, SERIES_SEED_BASES, path_key

VERSION = "3.0.0"
logger = logging.getLogger("potato")
_DEPLOY_SYNC_MARKER = "stream-proxy→potato-space"  # gate: potato deploy must mirror this source
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*")
RESOLVE_TTL = int(os.environ.get("RESOLVE_TTL", "240"))  # master-url resolve cache
HLS_TOKEN_TTL = int(os.environ.get("HLS_TOKEN_TTL", "21600"))  # signed url validity (6h)
UPSTREAM_TOKEN_TTL = int(os.environ.get("UPSTREAM_TOKEN_TTL", "180"))  # per-host token cache
CHUNK = 256 * 1024

@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.environ.get("INDEX_ON_START", "1") != "0":
        asyncio.create_task(_content_indexer.index_forever())
    yield
    if _client and not _client.is_closed:
        await _client.aclose()
    await _content_scraper.client.aclose()
    # also close backup sidecar client
    try:
        from backup import _close_client as _close_backup_client

        await _close_backup_client()
    except Exception:
        pass

app = FastAPI(title="FreakyMustard Proxy", version=VERSION, docs_url=None, redoc_url=None, lifespan=lifespan)
app.include_router(english_router)
app.include_router(backup_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if ALLOWED_ORIGINS == "*" else ALLOWED_ORIGINS.split(","),
    allow_credentials=False,
    allow_methods=["GET", "HEAD", "OPTIONS", "POST"],
    allow_headers=["*"],
    expose_headers=["Content-Range", "Accept-Ranges", "Content-Length"],
)

_cache = TTLCache(default_ttl=RESOLVE_TTL, max_entries=1024)

# Whole-file downloads are the heaviest thing this service does; cap how many
# run at once so one download storm can't starve playback on a free-tier CPU.
_DL_SEMAPHORE = asyncio.Semaphore(int(os.environ.get("DOWNLOAD_CONCURRENCY", "3")))

# --- content half (former Render backend) ------------------------------------

_content_scraper = MoviesdaScraper()
_content_indexer = MovieIndexer()

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


# --- download helpers ---------------------------------------------------------


def _variant_urls(text: str, base_url: str) -> list[tuple[int, str]]:
    """[(bandwidth, uri)] for every #EXT-X-STREAM-INF entry in a master playlist."""
    variants: list[tuple[int, str]] = []
    bw = 0
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#EXT-X-STREAM-INF"):
            m = re.search(r"BANDWIDTH=(\d+)", s)
            bw = int(m.group(1)) if m else 0
        elif s and not s.startswith("#"):
            variants.append((bw, urljoin(base_url, s)))
            bw = 0
    return variants


def _segment_urls(text: str, base_url: str) -> list[str]:
    """Absolute urls of every media segment line in a media playlist."""
    lines = [s.strip() for s in text.splitlines()]
    return [urljoin(base_url, s) for s in lines if s and not s.startswith("#")]


def _is_encrypted(text: str) -> bool:
    """True if the playlist demands EXT-X-KEY decryption we don't perform."""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#EXT-X-KEY") and "METHOD=NONE" not in s.upper():
            return True
    return False


def _safe_filename(name: str, ext: str = ".ts") -> str:
    name = re.sub(r"[^A-Za-z0-9._() -]+", "_", name).strip(" ._")
    return (name[:120] or "freakymustard") + ext


def _file_download_url(upstream: str, filename: str, request: Request) -> str:
    """Signed single-file download link (Tamil direct MP4/MKV relay)."""
    base = _public_base(request)
    token = sign_url(upstream, ttl=HLS_TOKEN_TTL)
    return f"{base}/file/{token}?filename={quote(filename or 'video')}"


def _pin_to_live_mirror(items: list[dict], scraper: MoviesdaScraper, live_base: str) -> list[dict]:
    """Rewrite catalogue urls that point at an old mirror onto ``live_base``.

    The cache can hand back a link — or a poster — on a domain that has since
    moved, while ``live_base`` is the domain that just served this listing, so
    that domain is the truth. Links go through the domain-independent path key
    (lossless); a poster is only touched when it sits on a mirror we have
    actually seen serving, so a third-party image host is left alone.
    """
    posters_rebaseable = {
        b
        for b in (scraper.resolved_base, *scraper.mirror_bases, *SERIES_SEED_BASES)
        if b and b != live_base
    }
    for item in items:
        key = path_key(item.get("link"))
        if key:
            item["link"] = canonical_link(live_base, key)
        poster = item.get("poster")
        if poster:
            parsed = urlparse(poster)
            if f"{parsed.scheme}://{parsed.netloc}" in posters_rebaseable:
                item["poster"] = urljoin(
                    live_base, parsed.path + (f"?{parsed.query}" if parsed.query else "")
                )
    return items


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
        "mongo": _content_indexer.collection is not None,
        "indexing": _content_indexer.is_indexing,
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


@app.get("/download/{token}")
async def download(token: str, request: Request, filename: str = "video"):
    """Assemble a resolved HLS stream into one MPEG-TS file and stream it out.

    Takes the same signed token the player uses (`/hls/{token}` pointing at a
    master playlist), picks the highest-bandwidth variant, and concatenates
    every media segment into a single `attachment` response. The browser then
    downloads natively (streamed to disk) instead of holding a movie-sized
    blob in tab memory.

    Segment bytes are relayed exactly as in /hls (segments are plain TS; the
    deep-health probe asserts the 0x47 sync byte). If the CDN's IP-bound token
    expires mid-download (TTL 180s), it is refreshed and the segment retried.
    """
    data = verify_token(token)
    if not data:
        raise HTTPException(403, "invalid or expired token")
    master_url = data["u"]
    if not _is_playlist(master_url):
        raise HTTPException(400, "token does not reference a playlist")

    parsed = urlparse(master_url)
    host_origin = f"{parsed.scheme}://{parsed.netloc}"
    client = await cdn()
    up_token = await _upstream_token(host_origin)

    async def fetch_playlist(url: str) -> str:
        try:
            resp = await client.get(
                _stamp_token(_strip_token(url), up_token), headers=_CDN_HEADERS
            )
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"upstream error: {exc}")
        if resp.status_code >= 400:
            raise HTTPException(resp.status_code, f"upstream {resp.status_code}")
        return resp.text

    master_text = await fetch_playlist(master_url)
    if _is_encrypted(master_text):
        raise HTTPException(422, "encrypted streams cannot be downloaded")

    variants = _variant_urls(master_text, master_url)
    if variants:
        # Highest bandwidth variant = best quality.
        variant_url = max(variants, key=lambda v: v[0])[1]
        media_text = await fetch_playlist(variant_url)
        if _is_encrypted(media_text):
            raise HTTPException(422, "encrypted streams cannot be downloaded")
    else:
        # The "master" was already a media playlist.
        variant_url = master_url
        media_text = master_text

    segments = _segment_urls(media_text, variant_url)
    if not segments:
        raise HTTPException(502, "no media segments found")

    safe = _safe_filename(filename)
    token_box = [up_token]  # mutable so the streamer can refresh it mid-flight

    async def assemble():
        # Failures before the first yield can still become proper HTTP errors;
        # after that the client simply sees a truncated download.
        async with _DL_SEMAPHORE:
            for seg in segments:
                for attempt in (0, 1):
                    try:
                        fetch_url = _stamp_token(_strip_token(seg), token_box[0])
                        req = client.build_request("GET", fetch_url, headers=_CDN_HEADERS)
                        resp = await client.send(req, stream=True)
                        if resp.status_code == 403 and attempt == 0:
                            # CDN token expired mid-download — refresh, retry.
                            await resp.aclose()
                            token_box[0] = await _upstream_token(host_origin)
                            continue
                        if resp.status_code >= 400:
                            await resp.aclose()
                            raise RuntimeError(f"segment http {resp.status_code}")
                        async for chunk in resp.aiter_bytes(CHUNK):
                            yield chunk
                        await resp.aclose()
                        break
                    except httpx.HTTPError:
                        if attempt:
                            raise
                        token_box[0] = await _upstream_token(host_origin)

    return StreamingResponse(
        assemble(),
        media_type="video/mp2t",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{safe}"; filename*=UTF-8\'\'{quote(safe)}'
            ),
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "no-store",
        },
    )


@app.get("/file/{token}")
async def file_relay(token: str, request: Request, filename: str = "video"):
    """Stream a resolved direct media file (Tamil MP4/MKV) as an attachment.

    Same signed-token contract as /hls, applied to single-file streams: the
    token embeds the exact upstream url (so no open proxy), bytes are relayed
    with bare headers (the CDN WAF 403s Origin/Referer), and Range requests
    pass through so browser downloads are resumable.
    """
    data = verify_token(token)
    if not data:
        raise HTTPException(403, "invalid or expired token")
    upstream_url = data["u"]

    client = await cdn()
    headers = dict(_CDN_HEADERS)
    rng = request.headers.get("range")
    if rng:
        headers["Range"] = rng
    try:
        req = client.build_request("GET", upstream_url, headers=headers)
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

    ext = os.path.splitext(urlparse(upstream_url).path)[1].lower()
    ext = ext if ext in (".mp4", ".mkv", ".webm", ".avi") else ".mp4"
    safe = _safe_filename(filename, ext)
    passthrough["Content-Disposition"] = (
        f'attachment; filename="{safe}"; filename*=UTF-8\'\'{quote(safe)}'
    )
    passthrough["Access-Control-Allow-Origin"] = "*"
    passthrough["Cache-Control"] = "no-store"

    async def streamer():
        try:
            async for chunk in resp.aiter_bytes(CHUNK):
                yield chunk
        finally:
            await resp.aclose()

    upstream_ct = resp.headers.get("content-type", "")
    media_type = upstream_ct if upstream_ct.startswith(("video/", "audio/")) else "video/mp4"
    return StreamingResponse(
        streamer(), status_code=resp.status_code, media_type=media_type, headers=passthrough
    )


# --- Tamil content routes (former Render backend) ----------------------------


@app.get("/api/search")
async def search_movies(q: str):
    """Search the indexed Tamil catalogue."""
    return await _content_indexer.search(q)


@app.get("/api/years")
async def get_years():
    """Tamil Level 1: year categories."""
    try:
        return await _content_scraper.get_years()
    except Exception as e:  # noqa: BLE001
        logger.exception("get_years failed")
        raise HTTPException(status_code=500, detail="failed to fetch year categories")


@app.get("/api/movies")
async def get_movies(year_url: str, pages: int = 3):
    """Tamil Level 2: movies for a year category, aggregating pages."""
    try:
        all_movies = []
        base_url = year_url
        if not base_url.endswith("/"):
            base_url += "/"

        for page_num in range(1, pages + 1):
            if page_num == 1:
                current_url = base_url
            else:
                separator = "?" if base_url.endswith("/") else "/?"
                current_url = f"{base_url}{separator}page={page_num}"

            try:
                movies = await _content_scraper.get_movies_in_year(current_url)
                all_movies.extend(movies)
            except Exception:
                break  # page doesn't exist

        return await _content_indexer.enrich_metadata(all_movies)
    except Exception as e:  # noqa: BLE001
        logger.exception("get_movies failed")
        raise HTTPException(status_code=500, detail="failed to fetch movies")


@app.get("/api/details")
async def get_movie_details(movie_url: str):
    """Tamil Level 3: quality variants + metadata for a movie."""
    try:
        data = await _content_scraper.get_qualities(movie_url)
        return data.get("qualities", [])
    except Exception as e:  # noqa: BLE001
        logger.exception("get_qualities failed")
        raise HTTPException(status_code=500, detail="failed to fetch qualities")


@app.get("/api/files")
async def get_files(quality_url: str):
    """Tamil Level 4: files inside a quality folder."""
    try:
        return await _content_scraper.get_files(quality_url)
    except Exception as e:  # noqa: BLE001
        logger.exception("get_files failed")
        raise HTTPException(status_code=500, detail="failed to fetch files")


@app.get("/api/stream")
async def get_stream_link(file_url: str, request: Request):
    """Tamil Levels 5-7: resolve the direct media link for a file page."""
    try:
        resolved = await _content_scraper.resolve_episode(file_url)
        if not resolved:
            raise HTTPException(status_code=404, detail="No working download server found")
        return {
            "stream_url": resolved["stream_url"],
            "download_url": _file_download_url(resolved["stream_url"], "video", request),
            "server_label": resolved["server_label"],
        }
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("get_stream_link failed")
        raise HTTPException(status_code=500, detail="failed to resolve stream")


@app.get("/api/auto-stream")
async def get_auto_stream(movie_url: str, request: Request):
    """Tamil: resolve the best stream for a movie in one call.

    Quality preference 1080p > 720p > …, then the largest file at the final
    level (size-optimised).
    """
    try:
        data = await _content_scraper.get_qualities(movie_url)
        qualities = data.get("qualities", [])
        if not qualities:
            raise HTTPException(status_code=404, detail="No qualities found")

        quality_priority = ["1080", "720", "640", "480", "original", "hd"]
        selected_quality = None
        for priority in quality_priority:
            for q in qualities:
                if priority in q["name"].lower():
                    selected_quality = q
                    break
            if selected_quality:
                break
        if not selected_quality:
            selected_quality = qualities[0]

        files = await _content_scraper.get_files(selected_quality["link"])
        if not files:
            raise HTTPException(status_code=404, detail="No files found")

        non_sample_files = [f for f in files if "sample" not in f["name"].lower()]
        candidates_l4 = non_sample_files if non_sample_files else files

        selected_file = None
        for priority in quality_priority:
            for f in candidates_l4:
                if priority in f["name"].lower():
                    selected_file = f
                    break
            if selected_file:
                break
        if not selected_file:
            selected_file = candidates_l4[0]

        servers = await _content_scraper.get_servers(selected_file["link"])
        if not servers:
            raise HTTPException(status_code=404, detail="No servers found")

        non_sample_servers = [s for s in servers if "sample" not in s["server"].lower()]
        candidates_l5 = non_sample_servers if non_sample_servers else servers

        def parse_size_mb(text: str) -> float:
            match = re.search(r"(\d+(?:\.\d+)?)\s*(GB|MB)", text, re.IGNORECASE)
            if not match:
                return 0.0
            val = float(match.group(1))
            return val * 1024 if match.group(2).upper() == "GB" else val

        target_server = max(candidates_l5, key=lambda s: parse_size_mb(s["server"]))

        final_link = await _content_scraper.resolve_final_link(target_server["link"], depth=0)
        if not final_link:
            raise HTTPException(status_code=404, detail="Could not resolve final link")

        return {
            "stream_url": final_link,
            "download_url": _file_download_url(
                final_link, selected_file["name"] or "video", request
            ),
            "quality": selected_quality["name"],
            "filename": selected_file["name"],
            "server_label": target_server["server"],
            "poster": data.get("meta", {}).get("poster"),
            "desc": data.get("meta", {}).get("desc"),
        }
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("auto-stream failed")
        raise HTTPException(status_code=500, detail="failed to resolve auto-stream")


@app.get("/api/series")
async def get_series(page: int = 1):
    """Tamil web series listing (paginated), enriched like the movie shelf.

    Two fixes over the original: the listing is now enriched with
    poster/description (cache first, one live detail scrape on a miss) so the
    shelf renders artwork instead of blank placeholders, and an unreachable
    upstream degrades to an explicit empty page instead of a 500 — the series
    shelf failing must not take the rest of the Tamil catalogue down with it.
    """
    try:
        results = await _content_scraper.get_series_list(page)
    except Exception as e:  # noqa: BLE001 — upstream, not a request error
        logger.warning("get_series: listing unavailable: %s", e)
        return {
            "page": page,
            "results": [],
            "has_more": False,
            "degraded": True,
            "error": f"{type(e).__name__}: {e}"[:300],
        }

    degraded = False
    try:
        results = await _content_indexer.enrich_metadata(results)
    except Exception as e:  # noqa: BLE001 — enrichment must never break the shelf
        # Bare {title, link} items are still a usable shelf; say so instead of
        # pretending the enrichment happened.
        logger.warning("get_series: enrichment failed: %s", e)
        degraded = True

    live_base = _content_scraper.series_base or _content_scraper.resolved_base
    if live_base:
        results = _pin_to_live_mirror(results, _content_scraper, live_base)

    return {
        "page": page,
        "results": results,
        "has_more": len(results) > 0,
        "degraded": degraded,
    }


@app.get("/api/seasons")
async def get_seasons(series_url: str):
    """Seasons (and metadata) for a Tamil web series."""
    try:
        data = await _content_scraper.get_seasons(series_url)
        return data
    except Exception as e:  # noqa: BLE001
        logger.exception("get_seasons failed")
        raise HTTPException(status_code=500, detail="failed to fetch seasons")


@app.get("/api/episodes")
async def get_episodes(season_url: str, pages: int = 10):
    """Episodes of a season, oldest first."""
    try:
        return await _content_scraper.get_episodes(season_url, pages)
    except Exception as e:  # noqa: BLE001
        logger.exception("get_episodes failed")
        raise HTTPException(status_code=500, detail="failed to fetch episodes")


@app.get("/api/episode-stream")
async def get_episode_stream(episode_url: str, request: Request):
    """Resolve a direct stream for one episode (tries every server)."""
    try:
        resolved = await _content_scraper.resolve_episode(episode_url)
        if not resolved:
            raise HTTPException(status_code=404, detail="Could not resolve episode stream")
        return {
            "stream_url": resolved["stream_url"],
            "download_url": _file_download_url(resolved["stream_url"], "episode", request),
            "server_label": resolved["server_label"],
        }
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("get_episode_stream failed")
        raise HTTPException(status_code=500, detail="failed to resolve episode stream")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "7860")))
