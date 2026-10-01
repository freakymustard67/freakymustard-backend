"""Backup streams (freaky-backup) integration.

Streamda's own resolver is VidSrc-with-an-HLS-byte-proxy. This module is the
*fallback* path: it proxies a sidecar ``freaky-backup`` service (a Node.js
Stremio-addon aggregator that also exposes a torrent->direct HTTP stream
engine) and normalises its output into the same shapes the frontend already
understands.

Two freaky-backup instances run as separate processes (English on :8101,
Tamil on :8102); we proxy through whichever instance the caller asks for.

Why proxy here instead of letting the browser hit freaky-backup directly?
  - freaky-backup's torrent->direct engine URLs are generated relative to the
    *calling* host, so a browser can never use them. We rewrite those /d/...
    URLs back through the same single origin (and relay the bytes), so a
    plain <video> can stream them with full Range support.
  - Keeps CORS / mixed-content to a single origin (the Space) like the rest of
    the app.

Endpoints
---------
GET  /backup/health
GET  /backup/manifest.json
GET  /backup/stream/{type}/{id}.json
GET  /api/backup/streams?type=&id=&season=&episode=&instance=
GET  /backup/d/{infoHash}/{fileIdx}/{name}   (Range passthrough, engine relay)

The engine /d/ relay is *not* HMAC-signed like /hls — abuse is bounded by
freaky-backup itself, which only services infoHashes that its aggregator
recently returned to a real request.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from urllib.parse import quote, urljoin, urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

router = APIRouter(tags=["Backup streams (freaky-backup)"])

# Upstream freaky-backup instances. Override with env in production.
BACKUP_ENGLISH = os.environ.get("BACKUP_ENGLISH", "http://127.0.0.1:8101").rstrip("/")
BACKUP_TAMIL = os.environ.get("BACKUP_TAMIL", "http://127.0.0.1:8102").rstrip("/")

# Direct site extractors run as their own service: a crawl is multi-hop and can
# take 30s+, which the sidecar's 18s fan-out cannot afford, and reachability
# depends on the region that service runs in. Empty = disabled.
SCRAPER_BASE = os.environ.get("SCRAPER_BASE", "").rstrip("/")
SCRAPER_BUDGET_MS = int(os.environ.get("SCRAPER_BUDGET_MS", "45000"))

_IMDB_RE = re.compile(r"^tt\d{4,10}$")
_CHUNK = 256 * 1024

# Matches a freaky-backup engine direct URL: .../d/<40-hex>[/<fileIdx>]/<name>
_ENGINE_URL_RE = re.compile(r"/d/([0-9a-fA-F]{40})(?:/(\d+))?(?:/.*)?$")

# Quality hints reused for ordering/labels (same spirit as freaky-backup).
_QUALITY = [
    (re.compile(r"\b(2160p|4k|uhd)\b", re.I), "2160p"),
    (re.compile(r"\b1080p\b", re.I), "1080p"),
    (re.compile(r"\b720p\b", re.I), "720p"),
    (re.compile(r"\b(480p|dvdrip|dvdr)\b", re.I), "480p"),
]
_EXT_FORMAT = {
    ".mkv": "mkv", ".mp4": "mp4", ".webm": "webm", ".m4v": "mp4",
    ".avi": "avi", ".mov": "mov", ".ts": "mp2t",
}

# --- playability classification --------------------------------------------
# A backup source is only useful if a browser <video> can actually play it.
# Three real-world shapes come out of the aggregators:
#   native   — MP4/WebM H.264: plays inline, right now
#   download — MKV/AVI/HEVC: a real video file the browser cannot decode
#   page     — a file-host *landing page* (HTML), never a video: needs a
#              host-specific resolver before it can play at all
_NATIVE_CONTAINERS = {"mp4", "m4v", "webm"}
_DOWNLOAD_CONTAINERS = {"mkv", "avi", "mov", "flv", "wmv", "mpg", "mpeg", "mp2t"}
_ALL_CONTAINERS = _NATIVE_CONTAINERS | _DOWNLOAD_CONTAINERS | {"ts"}

# Rank order used when sorting a title's backup sources.
_PLAY_ORDER = {"native": 0, "unknown": 1, "download": 2, "page": 3}

_HEVC_RE = re.compile(r"\b(x265|h\.?265|hevc|hvc1|dvh?|dolby\s*vision)\b", re.I)
_H264_RE = re.compile(r"\b(x264|h\.?264|avc)\b", re.I)
_SIZE_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(GB|GiB|MB|MiB)\b", re.I)
_SEED_RE = re.compile(r"(?:👤|👥|seeders?|peers?)\s*(\d+)", re.I)
_PAGE_HOST_RE = re.compile(
    r"(hubdrive|hubcloud|gdflix|filepress|drivebot|gdtot|filebee|sharer|"
    r"hubstream|hdhub|vcloud|katfile|filevault|links?\.)",
    re.I,
)

# Probe verdicts are cached — a page visit probes the same URLs repeatedly.
_PROBE_CACHE: dict[str, tuple[float, dict]] = {}
_PROBE_TTL = 300.0
_probe_sem: asyncio.Semaphore | None = None

_client: httpx.AsyncClient | None = None


def _public_base(request: Request) -> str:
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if base:
        return base
    # honour X-Forwarded-Proto when behind a TLS terminator (HF Spaces ingress)
    proto = request.headers.get("x-forwarded-proto")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if proto and host:
        return f"{proto}://{host}".rstrip("/")
    return str(request.base_url).rstrip("/")


def _base_for(instance: str) -> str:
    return BACKUP_TAMIL if instance == "tamil" else BACKUP_ENGLISH


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            follow_redirects=True, timeout=httpx.Timeout(45.0, read=300.0)
        )
    return _client


def _quality(blob: str) -> str:
    for re_, label in _QUALITY:
        if re_.search(blob):
            return label
    return ""


def _ext_format(url: str) -> str:
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    return _EXT_FORMAT.get(ext, "")


async def _close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
        _client = None


def _label(text: str) -> str:
    # strip the leading "[TAG] " that the aggregator prepends
    return re.sub(r"^\[[^\]]+\]\s*", "", (text or "").replace("\n", " · ")).strip()


def _container_from(url: str, filename: str = "") -> str:
    """Best-effort container guess from the filename, then the URL path."""
    for candidate in (filename or "", urlparse(url or "").path or ""):
        ext = os.path.splitext(candidate)[1].lower().lstrip(".")
        if ext in _ALL_CONTAINERS:
            return "mp2t" if ext == "ts" else ext
    return ""


def _quality_of(*texts: str) -> str:
    """Quality from the most specific text first (filename > title > blob)."""
    for text in texts:
        for pattern, label in _QUALITY:
            if pattern.search(text or ""):
                return label
    return ""


def _size_of(text: str) -> str:
    m = _SIZE_RE.search(text or "")
    return f"{m.group(1)} {m.group(2).upper()}" if m else ""


def _seeds_of(text: str) -> int:
    m = _SEED_RE.search(text or "")
    return int(m.group(1)) if m else 0


def _classify(url: str, filename: str, blob: str) -> dict:
    """Can a browser <video> play this inline? See the constants above."""
    # An extracted embed stream, relayed through our own proxy: a real HLS
    # playlist with the ad-laden player page removed. Nothing about its URL
    # looks like a media file, so it needs recognising explicitly — otherwise
    # it is written off as unknown and buried below downloads.
    if "/api/hls?t=" in (url or ""):
        return {
            "container": "hls",
            "codec": "",
            "playability": "native",
            "note": "ad-free HLS (player page bypassed)",
        }

    engine = bool(_ENGINE_URL_RE.search(url or ""))
    container = _container_from(url, filename)
    text = f"{filename or ''} {blob or ''}"
    hevc = bool(_HEVC_RE.search(text))
    h264 = bool(_H264_RE.search(text))

    if container in _NATIVE_CONTAINERS:
        if hevc:
            return {
                "container": container,
                "codec": "hevc",
                "playability": "download",
                "note": "HEVC — only some players decode this",
            }
        return {
            "container": container,
            "codec": "h264" if h264 else "",
            "playability": "native",
            "note": "",
        }

    if container in _DOWNLOAD_CONTAINERS:
        return {
            "container": container,
            "codec": "hevc" if hevc else ("h264" if h264 else ""),
            "playability": "download",
            "note": f"{container.upper()} — open in an external player",
        }

    if engine:
        # Torrent engines give us the real filename only sometimes; most of
        # what is left is MKV/HEVC, but we cannot know until playback starts.
        return {
            "container": "",
            "codec": "hevc" if hevc else "",
            "playability": "unknown",
            "note": "container known only once playback starts",
        }

    if _PAGE_HOST_RE.search(url or ""):
        return {
            "container": "",
            "codec": "",
            "playability": "page",
            "note": "file-host page — needs a host resolver",
        }

    return {"container": "", "codec": "", "playability": "unknown", "note": ""}


def _forward_headers(request: Request | None) -> dict:
    """Forward proto/host so backup sidecar generates correct https:// URLs."""
    if request is None:
        return {}
    h: dict[str, str] = {}
    # Preserve original scheme/host seen by the client
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if proto:
        h["x-forwarded-proto"] = proto
    if host:
        h["x-forwarded-host"] = host
        h["host"] = host
    return h


async def _fetch_json(base: str, path: str, request: Request | None = None) -> tuple[int, dict]:
    """GET an upstream freaky-backup endpoint, return (status, json)."""
    client = await _get_client()
    upstream = f"{base}{path}"
    headers = _forward_headers(request)
    try:
        resp = await client.get(upstream, headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"backup upstream unreachable: {exc}")
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, f"backup upstream {resp.status_code}")
    try:
        return resp.status_code, resp.json()
    except ValueError:
        raise HTTPException(502, "backup upstream returned non-JSON")


# ---- Stremio passthrough ---------------------------------------------------


@router.get("/backup/manifest.json")
async def backup_manifest(request: Request, instance: str = "english"):
    _, data = await _fetch_json(_base_for(instance), "/manifest.json", request)
    return JSONResponse(data)


@router.get("/backup/stream/{media_type}/{item_id}")
async def backup_stream_passthrough(
    request: Request, media_type: str, item_id: str, instance: str = "english", debug: int = 0
):
    """Stremio-shaped passthrough to a freaky-backup instance.

    Mirrors `/stream/{type}/{id}.json`. Accepts an optional trailing `.json`
    (Stremio sends it) and a `debug=1` flag. This lets a user install the
    backup addon directly from the Streamda origin too.
    """
    if media_type not in ("movie", "series", "anime"):
        raise HTTPException(400, "media_type must be movie|series|anime")
    if item_id.endswith(".json"):
        item_id = item_id[: -len(".json")]
    if not item_id:
        raise HTTPException(400, "missing id")
    qs = "?debug=1" if debug else ""
    _, data = await _fetch_json(
        _base_for(instance), f"/stream/{media_type}/{quote(item_id)}.json{qs}", request
    )
    return JSONResponse(data)


# ---- Normalised API for the frontend ---------------------------------------


@router.get("/api/backup/streams")
async def backup_streams(
    request: Request,
    type: str = "movie",
    id: str = "",
    season: int | None = None,
    episode: int | None = None,
    instance: str = "english",
    title: str = "",
    year: str = "",
):
    """Return playable direct sources (+ torrents) from a freaky-backup upstream.

    Movie  id : tt1234567
    Series id : tt1234567 + season + episode
    """
    id = id.strip()
    if not _IMDB_RE.match(id):
        raise HTTPException(400, "id must look like tt1234567")
    if type not in ("movie", "series"):
        raise HTTPException(400, "type must be movie|series")

    if type == "series":
        if season is None or episode is None:
            raise HTTPException(400, "season and episode are required for series")
        upstream_id = f"{id}:{season}:{episode}"
        media_type = "series"
    else:
        upstream_id = id
        media_type = "movie"

    # Site extractors search by name, so pass one through. Callers that already
    # know the title (the Tamil pages do) save a lookup; otherwise derive it.
    site_title = (title or "").strip()
    site_year = (year or "").strip()[:4]
    if not site_title:
        client = await _get_client()
        site_title, derived_year = await _title_for(id, media_type, client)
        site_year = site_year or derived_year

    query = f"/api/streams?type={type}&id={quote(upstream_id)}"
    if site_title:
        query += f"&title={quote(site_title)}&year={quote(site_year)}"

    # Both sources run concurrently: the scraper service is slower than the
    # addon sidecar, and stacking them would double the page's wait.
    sidecar_result, scraped = await asyncio.gather(
        _fetch_json(_base_for(instance), query, request),
        _scraper_streams(site_title, site_year, type, season, episode),
        return_exceptions=True,
    )
    if isinstance(scraped, BaseException):
        scraped = []

    if isinstance(sidecar_result, BaseException):
        exc = sidecar_result
        data = {"streams": []}
        # On HF the sidecars aren't deployed (no 8101/8102) — don't 502 the page,
        # just return an empty backup set so the main servers keep working.
        # The frontend shows "No backup streams" without a noisy 502 toast.
        # The addon sidecar is down. If the scraper service answered, serve
        # those rows anyway rather than showing the user an empty panel.
        if not scraped:
            return JSONResponse(
                {
                    "provider": "freaky-backup",
                    "instance": instance,
                    "mediaType": media_type,
                    "sourceId": upstream_id,
                    "sources": [],
                    "torrents": [],
                    "meta": {
                        "total": 0,
                        "playable": 0,
                        "maybe": 0,
                        "downloadOnly": 0,
                        "pages": 0,
                        "torrent": 0,
                        "error": str(getattr(exc, "detail", exc)),
                        "unavailable": True,
                    },
                }
            )
    else:
        _, data = sidecar_result

    raw_streams = list(data.get("streams", [])) + list(scraped)
    base = _public_base(request)

    sources: list[dict] = []
    torrents: list[dict] = []
    seen: set[str] = set()

    for s in raw_streams:
        if not isinstance(s, dict):
            continue
        blob = f"{s.get('name','')} {s.get('title','')} {s.get('description','')}"
        url = s.get("url")
        info_hash = s.get("infoHash")
        filename_hint = (s.get("behaviorHints") or {}).get("filename") or ""
        title = s.get("title") or ""
        # Aggregator titles are multi-line: filename / size / peers
        title_first = title.split("\n")[0].strip()

        if url:
            engine_match = _ENGINE_URL_RE.search(url)
            engine = bool(engine_match)

            if engine:
                # Engine rows carry a useless "[TAG] ⚡direct" name — the real
                # filename is what tells the user (and us) what the file is.
                if filename_hint and filename_hint.lower() != "stream.mp4":
                    raw_label = filename_hint
                elif title_first and "⚡direct" not in title_first:
                    raw_label = title_first
                else:
                    raw_label = filename_hint or s.get("name") or "Backup stream"
            else:
                # External rows read like "[4KHDHub] 1080p" — the host tag is
                # the only thing telling two similar rows apart, so keep it.
                raw_label = s.get("name") or title_first or "Backup stream"
            label = _label(raw_label)

            if engine:
                ih = engine_match.group(1).lower()
                idx = engine_match.group(2) or "0"
                name = quote(filename_hint or "stream.mp4")
                # rewrite into a browser-reachable URL on this origin
                out_url = f"{base}/backup/d/{ih}/{idx}/{name}"
                dedupe_key = f"engine:{ih}:{idx}"
            else:
                out_url = url
                # same file served by several upstreams: collapse on the path
                dedupe_key = f"url:{url.split('?')[0]}"
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)

            meta = _classify(url, filename_hint, blob)
            sources.append(
                {
                    "url": out_url,
                    "label": label,
                    "format": meta["container"] or _ext_format(url),
                    "quality": _quality_of(filename_hint, title_first, label, blob),
                    "size": _size_of(title),
                    "seeds": _seeds_of(title),
                    "engine": engine,
                    "playability": meta["playability"],
                    "container": meta["container"],
                    "codec": meta["codec"],
                    "note": meta["note"],
                    "host": urlparse(url).netloc.lower(),
                }
            )
        elif info_hash:
            if info_hash in seen:
                continue
            seen.add(info_hash)
            # raw torrent entry (no url) — surface as a magnet for downloaders
            magnet = s.get("magnet") or f"magnet:?xt=urn:btih:{info_hash}"
            torrents.append(
                {
                    "name": _label(s.get("name") or title_first) or "Torrent",
                    "magnet": magnet,
                    "infoHash": info_hash,
                    "fileIdx": s.get("fileIdx"),
                    "quality": _quality_of(title_first, blob),
                    "size": _size_of(title),
                    "seeds": _seeds_of(title),
                }
            )

    # Inline-playable first, then the unknown-but-worth-trying, then files that
    # need an external player, then host pages (which cannot play as-is).
    sources.sort(
        key=lambda x: (
            _PLAY_ORDER.get(x["playability"], 9),
            -_ql(x["quality"]),
            0 if x["engine"] else 1,
            x["label"].lower(),
        )
    )

    return {
        "provider": "freaky-backup",
        "instance": instance,
        "mediaType": media_type,
        "sourceId": upstream_id,
        "sources": sources,
        "torrents": torrents,
        "meta": {
            "total": len(sources) + len(torrents),
            "playable": sum(1 for x in sources if x["playability"] == "native"),
            "maybe": sum(1 for x in sources if x["playability"] == "unknown"),
            "downloadOnly": sum(1 for x in sources if x["playability"] == "download"),
            "pages": sum(1 for x in sources if x["playability"] == "page"),
            "torrent": len(torrents),
        },
    }


async def _scraper_streams(
    title: str, year: str, type: str, season: int | None, episode: int | None
) -> list[dict]:
    """Fetch rows from the standalone direct-site scraper service."""
    if not SCRAPER_BASE or not title:
        return []
    client = await _get_client()
    params = {"title": title, "type": type, "budget": str(SCRAPER_BUDGET_MS)}
    if year:
        params["year"] = year
    if type == "series":
        params["season"] = str(season or 1)
        params["episode"] = str(episode or 1)
    try:
        resp = await client.get(
            f"{SCRAPER_BASE}/api/scrape",
            params=params,
            timeout=httpx.Timeout(SCRAPER_BUDGET_MS / 1000 + 20, connect=10.0),
        )
        if resp.status_code != 200:
            return []
        return (resp.json() or {}).get("streams", []) or []
    except (httpx.HTTPError, ValueError):
        return []


def _ql(q: str) -> int:
    return {"2160p": 4, "1080p": 3, "720p": 2, "480p": 1}.get(q, 0)


# ---- Engine byte relay -----------------------------------------------------


@router.get("/backup/d/{info_hash}/{file_idx}/{name}")
async def backup_engine_file(
    request: Request,
    info_hash: str,
    file_idx: int = 0,
    name: str = "stream.mp4",
    instance: str = "english",
):
    """Relay a freaky-backup torrent->direct stream, honouring Range requests.

    The bytes come from webtorrent server-side on the backup sidecar; we just
    stream them out with Content-Range/Accept-Ranges passthrough so a browser
    <video> can play and seek a progressive file.
    """
    if not re.fullmatch(r"[0-9a-fA-F]{40}", info_hash):
        raise HTTPException(400, "invalid infoHash")
    base = _base_for(instance)
    # name arrives decoded by FastAPI; re-encode for upstream
    safe_name = quote(name, safe="._-")
    upstream = f"{base}/d/{info_hash.lower()}/{file_idx}/{safe_name}"
    # forward download hint to the sidecar as well (so it also sends attachment)
    if request.query_params.get("download") == "1":
        upstream += "?download=1"
    elif request.query_params.get("attachment") == "1":
        upstream += "?attachment=1"

    client = await _get_client()
    headers = {}
    rng = request.headers.get("range")
    if rng:
        headers["Range"] = rng
    # forward proto/host so engine logs correct origin (not critical for playback)
    headers.update({k: v for k, v in _forward_headers(request).items() if k.lower() != "host"})
    try:
        req = client.build_request("GET", upstream, headers=headers)
        resp = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"backup upstream error: {exc}")
    if resp.status_code >= 400:
        await resp.aclose()
        raise HTTPException(resp.status_code, f"backup upstream {resp.status_code}")

    passthrough = {}
    for h in ("content-length", "content-range", "accept-ranges", "content-type"):
        if h in resp.headers:
            passthrough[h] = resp.headers[h]
    passthrough.setdefault("Access-Control-Allow-Origin", "*")
    # Download vs player: engine normally sends `inline` so <video> can play.
    # When ?download=1 is present (triggerDownload for torrents), force `attachment`.
    if request.query_params.get("download") == "1" or request.query_params.get("attachment") == "1":
        safe_disp = (name or "video.mp4").replace('"', "").replace(";", "")[:150]
        passthrough["Content-Disposition"] = f'attachment; filename="{safe_disp}"; filename*=UTF-8\'\'{quote(safe_disp)}'
    elif "content-disposition" in resp.headers:
        passthrough["Content-Disposition"] = resp.headers["content-disposition"]

    async def streamer():
        try:
            async for chunk in resp.aiter_bytes(_CHUNK):
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(streamer(), status_code=resp.status_code, headers=passthrough)


# ---- Server-side reachability probe ----------------------------------------
#
# Why this exists: the browser cannot verify an external backup URL. A
# cross-origin HEAD without CORS headers rejects as an opaque TypeError, and
# the old client-side filter counted those as "working" — so every row claimed
# to be alive and almost every click failed. Probing here is same-origin (no
# CORS), and Content-Type separates a real video from a file-host HTML page.

_PROBE_CONCURRENCY = 10
_PROBE_MAX = 40
_PROBE_TIMEOUT = 7.0

# Content types that are video but that a browser <video> cannot decode.
_UNPLAYABLE_CONTENT_TYPES = (
    "matroska", "x-msvideo", "x-ms-wmv", "quicktime", "mp2t", "mpeg-ts",
)


class ProbeItem(BaseModel):
    key: str
    url: str
    engine: bool = False
    # Optional filename/label hint. Some CDNs (Cloudflare R2 presigned links in
    # particular) serve every object as application/octet-stream, so the
    # extension is the only thing that reveals whether it is an MP4 or an MKV.
    hint: str = ""


class ProbeRequest(BaseModel):
    items: list[ProbeItem] = Field(default_factory=list)


def _verdict_from(resp, hint: str = "") -> dict:
    ct = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    size = resp.headers.get("content-length") or ""
    status = resp.status_code
    if status >= 400:
        return {"status": "dead", "http": status, "contentType": ct, "size": size, "playable": False, "note": f"HTTP {status}"}
    if ct.startswith("video/"):
        # Browsers only reliably decode MP4/H.264 and WebM. Everything else
        # (matroska, AVI, QuickTime, raw MPEG-TS) is a real video file that
        # still needs an external player, so it must not be advertised as
        # click-to-play.
        playable = not any(bad in ct for bad in _UNPLAYABLE_CONTENT_TYPES)
        return {
            "status": "ok",
            "http": status,
            "contentType": ct,
            "size": size,
            "playable": playable,
            "note": "" if playable else f"{ct} needs an external player",
        }
    if ct in ("application/octet-stream", "binary/octet-stream", "application/x-matroska"):
        # Opaque type: fall back to the filename the aggregator gave us.
        container = _container_from(resp.headers.get("content-disposition", ""), hint)
        if container in _NATIVE_CONTAINERS:
            return {
                "status": "ok",
                "http": status,
                "contentType": ct,
                "size": size,
                "playable": True,
                "note": f"served as {ct} but is {container.upper()}",
            }
        return {
            "status": "ok",
            "http": status,
            "contentType": ct,
            "size": size,
            "playable": False,
            "note": f"opaque file — treat as {container.upper() or 'MKV'}",
        }
    if ct.startswith("text/html"):
        return {"status": "page", "http": status, "contentType": ct, "size": size, "playable": False, "note": "HTML page, not a video"}
    if ct.startswith("application/json") or ct.startswith("text/plain"):
        return {"status": "page", "http": status, "contentType": ct, "size": size, "playable": False, "note": "not a video file"}
    return {"status": "ok", "http": status, "contentType": ct, "size": size, "playable": False, "note": ""}


async def _probe_url(client: httpx.AsyncClient, url: str, hint: str = "") -> dict:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; FreakyMustard/1.0)", "Accept": "*/*"}
    try:
        resp = await client.head(url, headers=headers, follow_redirects=True, timeout=_PROBE_TIMEOUT)
        if resp.status_code < 400:
            return _verdict_from(resp, hint)
    except httpx.HTTPError:
        pass
    # Many file hosts reject HEAD (403/405/501) — Cloudflare R2 presigned links
    # are signed for GET only. Retry as a 2-byte ranged GET, streamed and never
    # read, so a host that ignores Range cannot make us download the whole file.
    try:
        req = client.build_request("GET", url, headers={**headers, "Range": "bytes=0-1"})
        resp = await client.send(req, stream=True)
        try:
            return _verdict_from(resp, hint)
        finally:
            await resp.aclose()
    except httpx.HTTPError as exc:
        return {"status": "dead", "playable": False, "note": str(exc)[:120]}


@router.post("/api/backup/probe")
async def backup_probe(payload: ProbeRequest):
    """Check which backup sources actually resolve to playable video."""
    global _probe_sem
    if _probe_sem is None:
        _probe_sem = asyncio.Semaphore(_PROBE_CONCURRENCY)
    client = await _get_client()
    items = payload.items[:_PROBE_MAX]

    async def run(item: ProbeItem):
        if item.engine:
            # Engine rows are streamed from the sidecar's torrent engine.
            # Probing one would start a torrent transfer, so don't.
            return item.key, {"status": "engine", "playable": True, "note": "starts on play"}
        now = time.monotonic()
        hit = _PROBE_CACHE.get(item.url)
        if hit and hit[0] > now:
            return item.key, hit[1]
        async with _probe_sem:
            verdict = await _probe_url(client, item.url, item.hint)
        _PROBE_CACHE[item.url] = (now + _PROBE_TTL, verdict)
        if len(_PROBE_CACHE) > 400:
            for k in list(_PROBE_CACHE)[:100]:
                _PROBE_CACHE.pop(k, None)
        return item.key, verdict

    pairs = await asyncio.gather(*(run(i) for i in items), return_exceptions=True)
    results: dict[str, dict] = {}
    for pair in pairs:
        if isinstance(pair, BaseException):
            continue
        key, verdict = pair
        results[key] = verdict
    return JSONResponse({"provider": "freaky-backup", "results": results})


@router.get("/api/backup/resolve")
async def backup_resolve(request: Request, url: str, instance: str = "english"):
    """Resolve a file-host landing page into a direct file URL.

    Done on demand rather than while listing: each resolution is a chain of
    HTTP hops (gate page -> token -> interstitial -> presigned CDN link), so we
    only pay for the row the user actually picked.
    """
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "url must be http(s)")
    base = _base_for(instance)
    try:
        _, data = await _fetch_json(base, f"/api/resolve?url={quote(url, safe='')}", request)
    except HTTPException as exc:
        return JSONResponse(
            {"provider": "freaky-backup", "ok": False, "error": str(getattr(exc, "detail", exc))}
        )
    return JSONResponse({"provider": "freaky-backup", **data})


# ---- Title -> IMDb id ------------------------------------------------------
#
# The Tamil catalogue is scraped pages with no IMDb id, but every backup addon
# (Torrentio, MediaFusion, DesiFlix) is keyed by IMDb id. Without this lookup
# the Tamil backup instance can never find anything.

_IMDB_CACHE: dict[str, tuple[float, dict]] = {}
_IMDB_TTL = 3600.0

# Scraped titles carry an enriched blob after the year:
#   "Hunkkaar The Roar (2026)7.8Cast:Devendra Patel...Genres:Crime, Thriller"
_META_BLOB_RE = re.compile(r"^(.*?\(\d{4}\))", re.S)


def _clean_scraped_title(raw: str) -> tuple[str, str]:
    """'Hunkkaar The Roar (2026)7.8Cast:…' -> ('Hunkkaar The Roar', '2026')."""
    text = (raw or "").strip()
    m = _META_BLOB_RE.match(text)
    if m:
        text = m.group(1)
    year = ""
    ym = re.search(r"\((\d{4})\)", text)
    if ym:
        year = ym.group(1)
        text = text.replace(ym.group(0), " ")
    text = re.sub(r"\s+", " ", text).strip(" -–—:.")
    return text, year


def _score_meta(meta: dict, name: str, year: str) -> int:
    cand = (meta.get("name") or "").strip().lower()
    want = name.lower()
    score = 0
    if cand == want:
        score += 5
    elif want and (want in cand or cand in want):
        score += 3
    elif want and _tokens_overlap(want, cand):
        score += 2
    cand_year = str(meta.get("releaseInfo") or meta.get("year") or "")[:4]
    if year and cand_year == year:
        score += 4
    elif year and cand_year.isdigit() and abs(int(cand_year) - int(year)) <= 1:
        score += 2
    if not (meta.get("imdb_id") or "").startswith("tt"):
        score -= 10
    return score


def _tokens_overlap(a: str, b: str) -> bool:
    ta = {t for t in re.split(r"\W+", a) if len(t) > 2}
    tb = {t for t in re.split(r"\W+", b) if len(t) > 2}
    return bool(ta & tb)


@router.get("/api/backup/imdb")
async def backup_imdb_lookup(title: str, year: str = "", type: str = "movie", region: str = ""):
    """Resolve a scraped title to an IMDb id so Tamil backups can be queried.

    Cinemeta's own search is useless for this: its catalogs are popularity-
    ranked and mostly Western, and the Tamil "Leo (2023)" does not appear for
    the query "leo" at all. IMDb's suggestion endpoint covers the whole of
    IMDb, and appending the year is what pulls regional titles up — "leo 2023"
    returns both Adam Sandler's Leo and Vijay's.

    Because name+year can still tie across regions, the Tamil pages pass
    region=in: tied candidates are then resolved through Cinemeta metadata and
    the Indian production wins.
    """
    from english import CINEMETA_BASE  # same directory, keeps the base in one place

    name, parsed_year = _clean_scraped_title(title)
    yr = (year or parsed_year or "").strip()[:4]
    if len(name) < 2:
        raise HTTPException(400, "title is too short to search")

    media = "series" if type == "series" else "movie"
    cache_key = f"{media}|{name.lower()}|{yr}|{region}"
    hit = _IMDB_CACHE.get(cache_key)
    if hit and hit[0] > time.monotonic():
        return JSONResponse(hit[1])

    client = await _get_client()
    candidates = await _imdb_suggest(client, name, yr)
    if not candidates:
        raise HTTPException(404, f"no IMDb match for '{name}'")

    ranked = sorted(
        candidates, key=lambda c: _score_suggestion(c, name, yr), reverse=True
    )[:3]
    best = ranked[0]

    # Regional tiebreak: several films can share a name and year.
    if region.lower() in ("in", "india") and len(ranked) > 1:
        best = await _prefer_indian(client, ranked, name, yr)

    result = {
        "imdbId": best.get("id"),
        "name": best.get("l"),
        "year": str(best.get("y") or ""),
        "cast": best.get("s"),
        "score": _score_suggestion(best, name, yr),
        "query": f"{name} {yr}".strip(),
        "source": "imdb-suggest",
    }
    _IMDB_CACHE[cache_key] = (time.monotonic() + _IMDB_TTL, result)
    if len(_IMDB_CACHE) > 500:
        for k in list(_IMDB_CACHE)[:100]:
            _IMDB_CACHE.pop(k, None)
    return JSONResponse(result)


_IMDB_SUGGEST_HOSTS = ("https://v2.sg.media-imdb.com", "https://v3.sg.media-imdb.com")
_VIDEO_TYPES = ("feature", "tv series", "tv mini-series", "tv movie", "video", "short")


_TITLE_CACHE: dict[str, tuple[float, str, str]] = {}
_TITLE_TTL = 3600.0


async def _title_for(imdb_id: str, media_type: str, client: httpx.AsyncClient) -> tuple[str, str]:
    """Resolve an IMDb id to (title, year).

    Direct site extractors search by *name*, not by id, so the id has to be
    turned back into a title before the sidecar can use them. Cached for an
    hour; returns ("", "") on any failure so the addon path still runs.
    """
    from english import CINEMETA_BASE

    hit = _TITLE_CACHE.get(imdb_id)
    if hit and hit[0] > time.monotonic():
        return hit[1], hit[2]
    try:
        resp = await client.get(f"{CINEMETA_BASE}/meta/{media_type}/{imdb_id}.json", timeout=10.0)
        if resp.status_code != 200:
            return "", ""
        meta = (resp.json() or {}).get("meta") or {}
        name = meta.get("name") or ""
        year = str(meta.get("releaseInfo") or meta.get("year") or "")[:4]
        _TITLE_CACHE[imdb_id] = (time.monotonic() + _TITLE_TTL, name, year)
        if len(_TITLE_CACHE) > 500:
            for k in list(_TITLE_CACHE)[:100]:
                _TITLE_CACHE.pop(k, None)
        return name, year
    except (httpx.HTTPError, ValueError):
        return "", ""


async def _imdb_suggest(client: httpx.AsyncClient, name: str, year: str) -> list[dict]:
    """Query IMDb autocomplete, year-first (that is what surfaces regional titles)."""
    from urllib.parse import quote as _q

    seen: dict[str, dict] = {}
    queries = [f"{name} {year}".strip(), name]
    for query in queries:
        q = query.lower()
        path = f"/suggestion/{_q(q[0])}/{_q(q.replace(' ', '_'))}.json"
        for host in _IMDB_SUGGEST_HOSTS:
            try:
                resp = await client.get(
                    f"{host}{path}",
                    headers={"User-Agent": "Mozilla/5.0 (compatible; FreakyMustard/1.0)"},
                    timeout=10.0,
                )
                if resp.status_code != 200:
                    continue
                for item in (resp.json() or {}).get("d", []) or []:
                    iid = item.get("id") or ""
                    if not iid.startswith("tt"):
                        continue
                    if (item.get("q") or "").lower() not in _VIDEO_TYPES:
                        continue
                    seen.setdefault(iid, item)
                break
            except (httpx.HTTPError, ValueError):
                continue
        if seen:
            break
    return list(seen.values())


def _score_suggestion(item: dict, name: str, year: str) -> int:
    cand = (item.get("l") or "").strip().lower()
    want = name.lower()
    score = 0
    if cand == want:
        score += 5
    elif want and (want in cand or cand in want):
        score += 3
    elif want and _tokens_overlap(want, cand):
        score += 2
    cand_year = str(item.get("y") or "")[:4]
    if year and cand_year == year:
        score += 4
    elif year and cand_year.isdigit() and abs(int(cand_year) - int(year)) <= 1:
        score += 2
    if (item.get("q") or "").lower() != "feature":
        score -= 1
    return score


async def _prefer_indian(
    client: httpx.AsyncClient, ranked: list[dict], name: str, year: str
) -> dict:
    """Of near-tied candidates, return the one produced in India (if any)."""
    from english import CINEMETA_BASE

    top_score = _score_suggestion(ranked[0], name, year)
    tied = [c for c in ranked if _score_suggestion(c, name, year) >= top_score - 4]
    for cand in tied:
        try:
            resp = await client.get(
                f"{CINEMETA_BASE}/meta/movie/{cand['id']}.json", timeout=8.0
            )
            if resp.status_code != 200:
                continue
            country = str(((resp.json() or {}).get("meta") or {}).get("country") or "")
            if "india" in country.lower():
                return cand
        except (httpx.HTTPError, ValueError):
            continue
    return ranked[0]


# ---- Health ----------------------------------------------------------------


@router.get("/backup/health")
async def backup_health(instance: str = "english"):
    base = _base_for(instance)
    client = await _get_client()
    status = {"ok": False, "instance": instance, "upstream": base}
    try:
        resp = await client.get(f"{base}/healthz", timeout=10.0)
        if resp.status_code == 200:
            body = resp.json()
            status["ok"] = bool(body.get("ok"))
            status["upstream_status"] = body
        else:
            status["error"] = f"upstream http {resp.status_code}"
    except (httpx.HTTPError, ValueError) as exc:
        status["error"] = str(exc)
    return JSONResponse({"provider": "freaky-backup", "fetch_time": int(time.time()), **status})
