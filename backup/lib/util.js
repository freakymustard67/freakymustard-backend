'use strict';
/**
 * freaky-backup · util.js
 * Logging, safe JSON fetching with timeouts, and Stremio id validation.
 * Zero dependencies (Node >= 18 global fetch).
 */

const UA = 'freaky-backup/1.0 (+https://localhost; stremio-addon-aggregator)';

function ts() {
  return new Date().toISOString().slice(11, 19);
}

function log(instance, ...args) {
  console.log(`[${ts()}] [${instance}]`, ...args);
}

/**
 * Fetch JSON with hard timeout + size sanity limit.
 * Returns { ok, status, data, ms, error } — never throws.
 */
async function fetchJson(url, timeoutMs = 12000) {
  const started = Date.now();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      signal: controller.signal,
      headers: {
        'User-Agent': UA,
        Accept: 'application/json, */*'
      },
      redirect: 'follow'
    });
    const ms = Date.now() - started;
    if (!res.ok) {
      return { ok: false, status: res.status, ms, error: `HTTP ${res.status}` };
    }
    const text = await res.text();
    try {
      return { ok: true, status: res.status, ms, data: JSON.parse(text) };
    } catch {
      return { ok: false, status: res.status, ms, error: 'non-JSON body' };
    }
  } catch (err) {
    const ms = Date.now() - started;
    const reason = err && err.name === 'AbortError' ? `timeout after ${timeoutMs}ms` : String(err.message || err);
    return { ok: false, status: 0, ms, error: reason };
  } finally {
    clearTimeout(timer);
  }
}

/** Only ever allow http(s) upstream URLs (SSRF guard). */
function isSafeHttpUrl(u) {
  try {
    const parsed = new URL(u);
    return parsed.protocol === 'http:' || parsed.protocol === 'https:';
  } catch {
    return false;
  }
}

/**
 * Validate a Stremio stream id for a given type and return a sanitized string
 * or null. Prevents path traversal / query injection into upstream URLs.
 *
 * Allowed shapes:
 *   movie : tt1254207 | tmdb:123 | dsf:<opaque>
 *   series: tt1254207:2:3 | tmdb:123:2:3 | dsf:<opaque>
 */
function validateId(type, rawId) {
  if (typeof rawId !== 'string') return null;
  const id = rawId.trim();
  if (!id || id.length > 512) return null;
  if (id.startsWith('dsf:')) {
    // opaque addon-internal id: restrict charset aggressively
    return /^dsf:[A-Za-z0-9_.:-]+$/.test(id) ? id : null;
  }
  if (/^tt\d+$/.test(id)) return type === 'series' ? `${id}:1:1` : id;
  if (/^tt\d+:\d+:\d+$/.test(id)) return type === 'series' ? id : null;
  if (/^tmdb:\d+$/.test(id)) return type === 'series' ? `${id}:1:1` : id;
  if (/^tmdb:\d+:\d+:\d+$/.test(id)) return type === 'series' ? id : null;
  return null;
}

const QUALITY_PATTERNS = [
  [/\b(2160p|4k|uhd)\b/i, 40],
  [/\b1080p\b/i, 30],
  [/\b720p\b/i, 20],
  [/\b480p\b|dvdr|\bdvdrip\b/i, 10],
  [/\b(cam|hdcam|dvdscr)\b/i, -25],
  [/hdr\+?\b|dv\b|dolby vision/i, 8],
  [/\bbluray\b|\bweb-?dl\b|\bweb\b/i, 6]
];

/** Rough quality score used only for ordering; never rejects streams. */
function qualityScore(stream) {
  const blob = `${stream.name || ''} ${stream.title || ''} ${stream.description || ''}`;
  let score = 0;
  for (const [re, pts] of QUALITY_PATTERNS) if (re.test(blob)) score += pts;
  if (/\bhindi\b.*\btamil\b|\btamil\b.*\bhindi\b|\bdual audio\b/i.test(blob)) score += 4;
  return score;
}

module.exports = { log, fetchJson, isSafeHttpUrl, validateId, qualityScore, UA };
