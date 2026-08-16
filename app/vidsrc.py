"""VidSrc (vidsrcme) HLS resolver.

Proven chain (verified live from residential AND datacenter IPs):

  1. GET https://data.vidsrcme.ru/api.php?type=<movie|tv>&imdb=<id>[&season&episode]&stream_urls
       -> JSON. ``data.stream_urls`` is an ENCRYPTED base64 blob and ``vs``
          carries a per-5-minute-window ChaCha20 WASM decryptor.
  2. Fetch ``vs.wasm_url`` -> WASM bytes.
  3. Run the WASM (``alloc`` + ``decrypt``) via wasmtime -> newline-separated
     HLS master-playlist URLs (no token yet).

The master/variant/segment bytes are then served by main.py's /hls proxy,
which appends an IP-bound upstream token and — critically — sends NO
Origin/Referer (the CDN's Cloudflare WAF 403s any request carrying them).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field

from curl_cffi import requests as cffi

API_BASE = "https://data.vidsrcme.ru/api.php"
API_REFERER = "https://vsembed.ru/"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)


class VidSrcError(Exception):
    pass


@dataclass
class StreamResult:
    provider: str = "vidsrc"
    media_type: str = "movie"
    source_id: str = ""
    title: str = ""
    imdb_id: str = ""
    # Raw HLS master playlist URLs (no token). Each is https://host/pl/.../master.m3u8
    masters: list[str] = field(default_factory=list)
    ttl: int = 240  # wasm window is ~5 min; keep resolve cache short


# --- WASM decryption -------------------------------------------------------

_wasm_cache: dict[str, object] = {}  # sha256(wasm bytes) -> compiled wasmtime.Module


def _decrypt_stream_urls(enc_b64: str, wasm_bytes: bytes) -> list[str]:
    """Run the provider's ChaCha20 WASM decryptor in-process via wasmtime."""
    try:
        from wasmtime import Engine, Instance, Module, Store
    except ImportError as exc:  # pragma: no cover
        raise VidSrcError("wasmtime not installed") from exc

    import ctypes

    key = hashlib.sha256(wasm_bytes).hexdigest()
    module = _wasm_cache.get(key)
    if module is None:
        engine = Engine()
        module = Module(engine, wasm_bytes)
        _wasm_cache[key] = module
        _wasm_cache.setdefault("__engine__", engine)

    engine = _wasm_cache["__engine__"]
    store = Store(engine)
    instance = Instance(store, module, [])
    ex = instance.exports(store)
    alloc = ex["alloc"]
    decrypt = ex["decrypt"]
    memory = ex["memory"]

    enc = base64.b64decode(enc_b64)
    ptr = alloc(store, len(enc))
    base = ctypes.addressof(memory.data_ptr(store).contents)
    dst = (ctypes.c_ubyte * len(enc)).from_address(base + ptr)
    for i, b in enumerate(enc):
        dst[i] = b

    out_len = decrypt(store, ptr, len(enc))
    out = (ctypes.c_ubyte * out_len).from_address(base + ptr + 12)
    text = bytes(out).decode("utf-8", errors="replace")
    return [line.strip() for line in text.split("\n") if line.strip()]


def _api_headers() -> dict:
    return {
        "User-Agent": UA,
        "Accept": "application/json",
        "Referer": API_REFERER,
    }


def _resolve(kind: str, imdb_id: str, season: int | None, episode: int | None) -> StreamResult:
    params = f"type={kind}&imdb={imdb_id}"
    if kind == "tv":
        params += f"&season={season}&episode={episode}"
    url = f"{API_BASE}?{params}&stream_urls"

    try:
        resp = cffi.get(url, headers=_api_headers(), impersonate="chrome131", timeout=30)
    except Exception as exc:
        raise VidSrcError(f"api request failed: {exc}") from exc

    if resp.status_code != 200:
        raise VidSrcError(f"api http {resp.status_code}")

    try:
        payload = resp.json()
    except Exception as exc:
        raise VidSrcError("api returned non-json") from exc

    data = payload.get("data") or {}
    stream_urls = data.get("stream_urls")
    if not stream_urls:
        raise VidSrcError("no stream_urls in api response (title may be unavailable)")

    # Backward-compat: sometimes it's already a plain list.
    if isinstance(stream_urls, list):
        masters = [u for u in stream_urls if isinstance(u, str) and u.startswith("http")]
    else:
        vs = payload.get("vs") or {}
        wasm_url = vs.get("wasm_url")
        wasm_b64 = vs.get("wasm")
        if wasm_url:
            try:
                wasm_bytes = cffi.get(
                    wasm_url, headers={"User-Agent": UA}, impersonate="chrome131", timeout=30
                ).content
            except Exception as exc:
                raise VidSrcError(f"wasm fetch failed: {exc}") from exc
        elif wasm_b64:
            wasm_bytes = base64.b64decode(wasm_b64)
        else:
            raise VidSrcError("stream_urls encrypted but no wasm provided")

        try:
            masters = _decrypt_stream_urls(stream_urls, wasm_bytes)
        except VidSrcError:
            raise
        except Exception as exc:
            raise VidSrcError(f"wasm decrypt failed: {exc}") from exc

    masters = [m for m in masters if m.startswith("http")]
    if not masters:
        raise VidSrcError("decryption produced no playable urls")

    return StreamResult(
        media_type=kind,
        source_id=imdb_id,
        title=data.get("title", ""),
        imdb_id=data.get("imdb_id", imdb_id),
        masters=masters,
    )


def resolve_movie(imdb_id: str) -> StreamResult:
    return _resolve("movie", imdb_id, None, None)


def resolve_tv(imdb_id: str, season: int, episode: int) -> StreamResult:
    return _resolve("tv", imdb_id, season, episode)
