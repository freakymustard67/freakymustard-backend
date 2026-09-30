'use strict';
/**
 * freaky-backup · upstream.js
 * Client for public Stremio addon stream endpoints.
 */

const { fetchJson, isSafeHttpUrl } = require('./util');
const { runSite, isSiteUpstream } = require('./sites');

/**
 * Fetch `{ streams: [...] }` from one upstream for a type/id pair.
 * Returns { ok, ms, status, streams, error } — never throws.
 *
 * Two kinds of upstream:
 *   - Stremio addons, addressed by `baseUrl` and keyed by IMDb id
 *   - direct site extractors (`driver: "site"`), which search the site by
 *     *title* and therefore need `ctx.title` / `ctx.year`
 *
 * @param {{name:string, tag:string, baseUrl?:string, driver?:string, torrent?:boolean, optional?:boolean}} upstream
 * @param {{title?:string, year?:string}} [ctx]
 */
async function fetchStreams(upstream, type, id, timeoutMs, ctx = {}) {
  if (isSiteUpstream(upstream)) {
    const res = await runSite(upstream, { ...ctx, type }, timeoutMs);
    return { ok: res.ok, streams: res.streams, ms: res.ms, error: res.error, searched: res.searched };
  }

  const base = (upstream.baseUrl || '').replace(/\/+$/, '');
  const url = `${base}/stream/${encodeURIComponent(type)}/${encodeURIComponent(id)}.json`;
  if (!isSafeHttpUrl(url)) {
    return { ok: false, streams: [], error: 'unsafe upstream url' };
  }
  const res = await fetchJson(url, timeoutMs);
  if (!res.ok) {
    return { ok: false, streams: [], ms: res.ms, error: res.error };
  }
  const raw = Array.isArray(res.data && res.data.streams) ? res.data.streams : [];
  // keep only well-formed entries
  const streams = [];
  for (const s of raw) {
    if (!s || typeof s !== 'object') continue;
    if (!s.url && !s.infoHash && !s.ytId && !s.externalUrl) continue;
    if (s.url && !isSafeHttpUrl(s.url)) continue;
    streams.push(s);
  }
  return { ok: true, streams, ms: res.ms, total: raw.length };
}

/** Fetch a manifest to verify reachability + capture real addon name. */
async function fetchManifest(upstream, timeoutMs = 10000) {
  const base = (upstream.baseUrl || '').replace(/\/+$/, '');
  if (!isSafeHttpUrl(`${base}/manifest.json`)) return { ok: false, error: 'unsafe url' };
  const res = await fetchJson(`${base}/manifest.json`, timeoutMs);
  if (!res.ok) return res;
  return { ...res, name: res.data && res.data.name, id: res.data && res.data.id };
}

module.exports = { fetchStreams, fetchManifest };
