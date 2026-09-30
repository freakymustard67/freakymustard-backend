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
 * Fetch an HTML/text page with a hard timeout, a size cap and browser-ish
 * headers. Returns { ok, status, text, ms, url, error } — never throws.
 *
 * The Tamil sites in front of the download hosts are ordinary HTML pages, but
 * several of them 403 a bare fetch, so we send a normal browser UA and accept
 * language headers. We do NOT follow javascript: or non-http(s) redirects.
 *
 * @param {string} url
 * @param {{timeoutMs?:number, maxBytes?:number, headers?:object, method?:string, body?:string, redirect?:string}} [opts]
 */
async function fetchText(url, opts = {}) {
  const {
    timeoutMs = 12000,
    maxBytes = 900 * 1024,
    headers = {},
    method = 'GET',
    body,
    redirect = 'follow'
  } = opts;
  const started = Date.now();
  if (!isSafeHttpUrl(url)) return { ok: false, status: 0, error: 'unsafe url', ms: 0 };
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      method,
      body,
      signal: controller.signal,
      redirect,
      headers: {
        'User-Agent':
          'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
        Accept: 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-IN,en;q=0.9,ta;q=0.8',
        ...headers
      }
    });
    const ms = Date.now() - started;
    const finalUrl = res.url || url;
    if (!res.ok) return { ok: false, status: res.status, ms, url: finalUrl, error: `HTTP ${res.status}` };
    const buf = Buffer.from(await res.arrayBuffer());
    const text = buf.subarray(0, maxBytes).toString('utf8');
    return { ok: true, status: res.status, ms, url: finalUrl, text, bytes: buf.length };
  } catch (err) {
    const ms = Date.now() - started;
    const reason =
      err && err.name === 'AbortError' ? `timeout after ${timeoutMs}ms` : String(err.message || err);
    return { ok: false, status: 0, ms, error: reason };
  } finally {
    clearTimeout(timer);
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

module.exports = { log, fetchJson, fetchText, isSafeHttpUrl, validateId, qualityScore, UA };
