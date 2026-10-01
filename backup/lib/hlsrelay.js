'use strict';
/**
 * freaky-backup · hlsrelay.js
 *
 * Signing and playlist rewriting for the HLS relay.
 *
 * The raw playlists these embed players hand out are signed with the
 * requesting network — the query carries `asn=`/IP — so the identical URL
 * returns 206 from the machine that minted it and 403 from anywhere else,
 * including our own backend. The bytes therefore have to be relayed by whoever
 * extracted them, and the relay has to be addressed with an HMAC token rather
 * than a bare URL so it cannot be used as an open proxy.
 *
 * Shared by the HTTP route and the site driver: minting the token in only one
 * of those places is exactly the bug that made extracted streams come out with
 * an "undefined" URL.
 */

const crypto = require('crypto');

const SECRET = process.env.SCRAPER_SECRET || 'freaky-scrapers-local-secret';

function signToken(url) {
  const body = Buffer.from(String(url), 'utf8').toString('base64url');
  const sig = crypto.createHmac('sha256', SECRET).update(body).digest('hex').slice(0, 32);
  return `${body}.${sig}`;
}

function verifyToken(token) {
  const i = String(token || '').lastIndexOf('.');
  if (i <= 0) return null;
  const body = token.slice(0, i);
  const sig = token.slice(i + 1);
  const want = crypto.createHmac('sha256', SECRET).update(body).digest('hex').slice(0, 32);
  if (sig.length !== want.length) return null;
  if (!crypto.timingSafeEqual(Buffer.from(sig), Buffer.from(want))) return null;
  try {
    const url = Buffer.from(body, 'base64url').toString('utf8');
    return /^https?:\/\//i.test(url) ? url : null;
  } catch {
    return null;
  }
}

/** Path (not absolute) so the caller decides which origin to publish. */
function relayPathFor(mediaUrl, referer) {
  if (!mediaUrl) return null;
  const ref = referer ? `&ref=${encodeURIComponent(referer)}` : '';
  return `/api/hls?t=${signToken(mediaUrl)}${ref}`;
}

const PLAYLIST_TYPES = /application\/(vnd\.apple\.mpegurl|x-mpegurl)|audio\/mpegurl/i;

/** Rewrite a playlist so every variant and segment comes back through us. */
function rewritePlaylist(text, baseUrl) {
  const abs = (u) => {
    try {
      return new URL(u, baseUrl).toString();
    } catch {
      return null;
    }
  };
  return String(text)
    .split('\n')
    .map((line) => {
      const t = line.trim();
      if (!t) return line;
      if (t.startsWith('#')) {
        return line.replace(/URI="([^"]+)"/g, (m, u) => {
          const a = abs(u);
          return a ? `URI="${relayPathFor(a)}"` : m;
        });
      }
      const a = abs(t);
      return a ? relayPathFor(a) : line;
    })
    .join('\n');
}

module.exports = { signToken, verifyToken, relayPathFor, rewritePlaylist, PLAYLIST_TYPES };
