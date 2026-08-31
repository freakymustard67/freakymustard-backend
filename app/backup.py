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

import os
import re
import time
from urllib.parse import quote, urljoin, urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

router = APIRouter(tags=["Backup streams (freaky-backup)"])

# Upstream freaky-backup instances. Override with env in production.
BACKUP_ENGLISH = os.environ.get("BACKUP_ENGLISH", "http://127.0.0.1:8101").rstrip("/")
BACKUP_TAMIL = os.environ.get("BACKUP_TAMIL", "http://127.0.0.1:8102").rstrip("/")

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

    try:
        _, data = await _fetch_json(
            _base_for(instance), f"/api/streams?type={type}&id={quote(upstream_id)}", request
        )
    except HTTPException as exc:
        # On HF the sidecars aren't deployed (no 8101/8102) — don't 502 the page,
        # just return an empty backup set so the main servers keep working.
        # The frontend shows "No backup streams" without a noisy 502 toast.
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
                    "torrent": 0,
                    "error": str(getattr(exc, "detail", exc)),
                    "unavailable": True,
                },
            }
        )

    raw_streams = data.get("streams", [])
    base = _public_base(request)

    sources: list[dict] = []
    torrents: list[dict] = []
    for s in raw_streams:
        if not isinstance(s, dict):
            continue
        blob = f"{s.get('name','')} {s.get('title','')} {s.get('description','')}"
        url = s.get("url")
        info_hash = s.get("infoHash")
        # For engine streams the upstream name is just "[TAG] ⚡direct" – use filename/title instead
        raw_label = s.get("name") or s.get("title") or "Backup stream"
        filename_hint = (s.get("behaviorHints") or {}).get("filename") or ""
        if url and _ENGINE_URL_RE.search(url):
            # Prefer filename, then title's first line (before peers/size)
            title_first = (s.get("title") or "").split("\n")[0].strip()
            if filename_hint and filename_hint.lower() != "stream.mp4":
                raw_label = filename_hint
            elif title_first and "⚡direct" not in title_first:
                raw_label = title_first
            elif filename_hint:
                raw_label = filename_hint
        label = _label(raw_label)

        if url:
            m = _ENGINE_URL_RE.search(url)
            if m:
                ih = m.group(1).lower()
                idx = m.group(2) or "0"
                name = quote(filename_hint or "stream.mp4")
                # rewrite into a browser-reachable URL on this origin
                out_url = f"{base}/backup/d/{ih}/{idx}/{name}"
                kind = "engine"
            else:
                out_url = url
                kind = "direct"
            sources.append(
                {
                    "url": out_url,
                    "label": label,
                    "format": _ext_format(url),
                    "quality": _quality(blob),
                    "engine": kind == "engine",
                }
            )
        elif info_hash:
            # raw torrent entry (no url) — surface as a magnet for downloaders
            magnet = s.get("magnet") or f"magnet:?xt=urn:btih:{info_hash}"
            torrents.append(
                {
                    "name": label,
                    "magnet": magnet,
                    "infoHash": info_hash,
                    "fileIdx": s.get("fileIdx"),
                    "quality": _quality(blob),
                }
            )

    # Playable direct sources first (engines before externals), then best quality.
    sources.sort(key=lambda x: (0 if x["engine"] else 1, -_ql(x["quality"])))

    return {
        "provider": "freaky-backup",
        "instance": instance,
        "mediaType": media_type,
        "sourceId": upstream_id,
        "sources": sources,
        "torrents": torrents,
        "meta": {
            "total": len(sources) + len(torrents),
            "playable": len(sources),
            "torrent": len(torrents),
        },
    }


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
