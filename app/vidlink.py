"""VidLink.pro stream extraction.

Reverse-engineered encrypted API (XSalsa20-Poly1305 via PyNaCl).
Accepts IMDB ids directly — no TMDB mapping required.

Reference: github.com/walterwhite-69/Vidlink.pro-Decryptor (MIT-style,
keys are public in vidlink's own site assets).
"""

from __future__ import annotations

import base64
import struct
import time
from dataclasses import dataclass, field

import nacl.secret
from curl_cffi import requests as cffi

# Production key extracted from vidlink.pro's public JS bundle.
_KEY_HEX = "c75136c5668bbfe65a7ecad431a745db68b5f381555b38d8f6c699449cf11fcd"
_BOX = nacl.secret.SecretBox(bytes.fromhex(_KEY_HEX))
_NONCE = bytes(24)

_API_BASE = "https://vidlink.pro/api/b"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
    ),
    "Origin": "https://vidlink.pro",
    "Referer": "https://vidlink.pro/",
}
_IMPERSONATE = "chrome131"

# Referer/Origin the media CDN requires (verified: 403/428 without it).
CDN_REFERER = "https://vidlink.pro/"
CDN_ORIGIN = "https://vidlink.pro"


class VidLinkError(RuntimeError):
    """Raised when extraction fails."""


@dataclass
class Quality:
    label: str          # "360" | "480" | "720" | "1080" ...
    url: str            # direct CDN url (signed, time-limited)
    size: int = 0       # bytes
    codec: str = ""     # hevc | h264 ...
    fmt: str = "mp4"    # mp4 | hls


@dataclass
class Caption:
    label: str
    url: str


@dataclass
class StreamResult:
    provider: str = "vidlink"
    media_type: str = "file"      # file | hls
    qualities: list[Quality] = field(default_factory=list)
    captions: list[Caption] = field(default_factory=list)
    ttl: int = 3600               # seconds the CDN urls stay valid
    source_id: str = ""


def _encrypt_token(media_id: str) -> str:
    """Build vidlink's encrypted path token for a media id."""
    timestamp = int(time.time() + 480)  # their player uses a +8min skew
    message = media_id.encode("utf-8") + struct.pack(">Q", timestamp)
    encrypted = _BOX.encrypt(message, _NONCE)
    payload = _NONCE + encrypted.ciphertext
    return base64.urlsafe_b64encode(payload).decode("utf-8").rstrip("=")


def _fetch(path: str) -> dict:
    url = f"{_API_BASE}/{path}?multiLang=1"
    try:
        resp = cffi.get(
            url, headers=_HEADERS, impersonate=_IMPERSONATE, timeout=30
        )
    except Exception as exc:  # network-level failure
        raise VidLinkError(f"vidlink request failed: {exc}") from exc

    if resp.status_code != 200:
        raise VidLinkError(f"vidlink api http {resp.status_code}")
    try:
        data = resp.json()
    except Exception as exc:
        raise VidLinkError("vidlink returned non-json payload") from exc
    if not data or "stream" not in data:
        raise VidLinkError("vidlink returned no stream for this id")
    return data


def _parse(data: dict) -> StreamResult:
    stream = data.get("stream", {})
    result = StreamResult(
        media_type=stream.get("type", "file"),
        ttl=int(stream.get("TTL") or 3600),
        source_id=str(data.get("sourceId", "")),
    )

    qualities = stream.get("qualities") or {}
    for label, info in qualities.items():
        url = info.get("url")
        if not url:
            continue
        result.qualities.append(
            Quality(
                label=str(label),
                url=url,
                size=int(info.get("size") or 0),
                codec=str(info.get("codecName") or ""),
                fmt=info.get("type") or "mp4",
            )
        )
    # Best quality first.
    result.qualities.sort(key=lambda q: int(q.label) if q.label.isdigit() else 0, reverse=True)

    # Single HLS url variant (some sources deliver hls instead of files).
    if not result.qualities and stream.get("url"):
        result.qualities.append(Quality(label="auto", url=stream["url"], fmt="hls"))
        result.media_type = "hls"

    for cap in stream.get("captions") or []:
        if cap.get("url"):
            result.captions.append(
                Caption(label=cap.get("label") or cap.get("lang") or "sub", url=cap["url"])
            )

    if not result.qualities:
        raise VidLinkError("vidlink stream had no playable urls")
    return result


def resolve_movie(imdb_id: str) -> StreamResult:
    return _parse(_fetch(f"movie/{_encrypt_token(imdb_id)}"))


def resolve_tv(imdb_id: str, season: int, episode: int) -> StreamResult:
    return _parse(_fetch(f"tv/{_encrypt_token(imdb_id)}/{season}/{episode}"))
