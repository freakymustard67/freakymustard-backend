"""Signed, expiring URL tokens.

The /media and /caption endpoints accept only urls that WE signed, so the
Space can never be abused as an open proxy. Tokens are short-lived and
bound to the exact upstream url.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

_SECRET = os.environ.get("PROXY_SECRET", "streamda-default-secret-change-me").encode()
_TOKEN_TTL = int(os.environ.get("PROXY_TOKEN_TTL", "3600"))  # match CDN url ttl


def _mac(payload: bytes) -> str:
    return hmac.new(_SECRET, payload, hashlib.sha256).hexdigest()[:32]


def sign_url(url: str, ttl: int | None = None, referer: str = "") -> str:
    """Return an opaque token embedding url + expiry + referer hint."""
    expires = int(time.time()) + (ttl or _TOKEN_TTL)
    body = json.dumps({"u": url, "e": expires, "r": referer}, separators=(",", ":")).encode()
    b64 = base64.urlsafe_b64encode(body).decode().rstrip("=")
    return f"{b64}.{_mac(body)}"


def verify_token(token: str) -> dict | None:
    """Return {'u': url, 'e': expires, 'r': referer} or None if invalid/expired."""
    try:
        b64, mac = token.rsplit(".", 1)
        pad = "=" * (-len(b64) % 4)
        body = base64.urlsafe_b64decode(b64 + pad)
    except Exception:
        return None
    if not hmac.compare_digest(_mac(body), mac):
        return None
    try:
        data = json.loads(body)
    except Exception:
        return None
    if not isinstance(data.get("u"), str) or int(data.get("e", 0)) < time.time():
        return None
    return data
