'use strict';
/**
 * freaky-backup · scraper-server.js
 *
 * Standalone extraction service for the direct site scrapers.
 *
 * Why this is its own deployment rather than living inside the main sidecar:
 *
 *  - **Latency budget.** A site extraction is a multi-hop crawl (search ->
 *    film page -> /original/ -> /1080p-hd/ -> /download/) and can take 20-40s.
 *    The main sidecar has to answer inside an 18s fan-out deadline shared with
 *    the fast addons, so slow crawls were being cut off and contributing
 *    nothing. Here they get their own budget.
 *  - **Region.** These sites are blocked from Indian residential ISPs by court
 *    order and are sensitive to which datacenter ASN/region asks. A separate
 *    service can be placed in whichever region can actually reach them,
 *    independent of where the streaming proxy runs.
 *  - **Blast radius.** A hanging site cannot slow down or OOM the service that
 *    is actually serving video.
 *
 * Deliberately dependency-free (Node >= 18 global fetch) so it installs in
 * seconds and stays small.
 */

const http = require('http');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const { runSite, isSiteUpstream, findMagnets } = require('./lib/sites');
const { fetchText } = require('./lib/util');
const { resolveHost } = require('./lib/resolvers');
const { extractMedia } = require('./lib/extractors');

const PORT = Number(process.env.PORT || 8080);
const CONFIG_PATH = process.env.SCRAPERS_CONFIG || path.join(__dirname, 'config', 'scrapers.json');
const UA_TAG = 'freaky-scrapers/1.0';

function loadSites() {
  try {
    const raw = fs.readFileSync(CONFIG_PATH, 'utf8');
    const cfg = JSON.parse(raw);
    const sites = (cfg.sites || cfg.upstreams || []).filter(isSiteUpstream);
    return sites;
  } catch (err) {
    console.error(`[scrapers] config load failed (${CONFIG_PATH}): ${err.message}`);
    return [];
  }
}

let SITES = loadSites();
let INSPECT_FAMILIES = [];
try {
  const raw = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8'));
  INSPECT_FAMILIES = raw.inspectFamilies || [];
} catch {
  INSPECT_FAMILIES = [];
}
const startedAt = Date.now();
const lastResult = new Map(); // site name -> { ok, count, ms, error, at }
const lastExtract = new Map(); // embed host -> last extraction result

/**
 * Is this URL within a site family we already scrape?
 *
 * The inspect/probe helpers exist to keep drivers alive as these sites rotate
 * their domains, so they must accept a *new* mirror of a site we already
 * target — but nothing else, or they become an open proxy. Matching is on the
 * family token (movierulz, tamilgun, ...) taken from the configured domains.
 */
function familyTokens() {
  const toks = new Set();
  for (const s of SITES) {
    for (const d of s.domains || []) {
      const host = String(d).replace(/^https?:\/\//, '').replace(/\/.*$/, '');
      const core = host.replace(/^www\./, '').split('.')[0];
      if (core && core.length > 3) toks.add(core);
    }
  }
  for (const t of INSPECT_FAMILIES) if (t) toks.add(String(t).toLowerCase());
  return [...toks];
}

function inFamily(target) {
  const lower = String(target).toLowerCase();
  if (SITES.some((s) =>
    [...(s.domains || []), ...(s.linkHosts || [])].some((n) =>
      lower.includes(String(n).replace(/^https?:\/\//, '').replace(/\/.*$/, '').toLowerCase())
    )
  )) return true;
  return familyTokens().some((t) => lower.includes(t));
}

function hostOf(u) {
  try {
    return new URL(String(u)).hostname.replace(/^www\./, '');
  } catch {
    return '';
  }
}

function json(res, code, body) {
  const payload = JSON.stringify(body);
  res.writeHead(code, {
    'Content-Type': 'application/json; charset=utf-8',
    'Access-Control-Allow-Origin': '*',
    'Cache-Control': 'no-store'
  });
  res.end(payload);
}

/**
 * Run every configured site in parallel, each with its own timeout, and return
 * whatever answered. One slow site never blocks the others: each has its own
 * deadline and a failure is just an absent source.
 */
async function scrapeAll(ctx, perSiteTimeoutMs) {
  const started = Date.now();
  const results = await Promise.all(
    SITES.map(async (site) => {
      const t0 = Date.now();
      try {
        const r = await runSite(site, ctx, perSiteTimeoutMs);
        lastResult.set(site.name, {
          ok: r.ok,
          count: r.streams.length,
          ms: r.ms,
          error: r.error || null,
          searched: r.searched || null,
          matched: r.matched || null,
          extracted: r.extracted != null ? r.extracted : null,
          extractTried: r.extractTried || null,
          at: Date.now()
        });
        return r.streams || [];
      } catch (err) {
        lastResult.set(site.name, {
          ok: false,
          count: 0,
          ms: Date.now() - t0,
          error: String(err.message || err),
          at: Date.now()
        });
        return [];
      }
    })
  );

  // De-duplicate across sites: two sites often carry the same host link.
  // A torrent has no `url` (it is an infoHash), so it needs its own key —
  // keying on url alone silently drops every magnet here, after the driver has
  // already found it.
  const seen = new Set();
  const streams = [];
  for (const list of results) {
    for (const s of list) {
      const key = s.url ? s.url.split('?')[0] : s.infoHash ? `magnet:${s.infoHash}` : '';
      if (!key || seen.has(key)) continue;
      seen.add(key);
      streams.push(s);
    }
  }
  return { streams, ms: Date.now() - started };
}

const server = http.createServer((req, res) => {
  handle(req, res).catch((err) => {
    // An async handler that throws is an unhandled rejection, which kills the
    // process - one bad route once crash-looped this whole service. Fail the
    // request instead.
    console.error(`[scrapers] unhandled route error: ${err && err.stack ? err.stack : err}`);
    try {
      json(res, 500, { error: 'internal error', detail: String((err && err.message) || err).slice(0, 200) });
    } catch {
      try { res.destroy(); } catch { /* already gone */ }
    }
  });
});

const { verifyToken, relayPathFor, rewritePlaylist, PLAYLIST_TYPES } = require('./lib/hlsrelay');

async function handle(req, res) {
  const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);

  if (req.method === 'OPTIONS') {
    res.writeHead(204, {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, OPTIONS',
      'Access-Control-Allow-Headers': '*'
    });
    res.end();
    return;
  }

  if (url.pathname === '/health' || url.pathname === '/healthz') {
    json(res, 200, {
      ok: true,
      service: UA_TAG,
      uptimeSec: Math.round((Date.now() - startedAt) / 1000),
      sites: SITES.map((s) => s.name),
      lastExtract: Object.fromEntries(
        [...lastExtract.entries()].map(([k, v]) => [
          k,
          { ok: v.ok, kind: v.kind || null, host: v.host || null, error: v.error || null },
        ])
      ),
      lastResult: Object.fromEntries([...lastResult.entries()])
    });
    return;
  }

  if (url.pathname === '/api/scrape') {
    const title = (url.searchParams.get('title') || '').trim();
    const year = (url.searchParams.get('year') || '').trim().slice(0, 4);
    const type = url.searchParams.get('type') === 'series' ? 'series' : 'movie';
    const season = Number(url.searchParams.get('season') || 0) || undefined;
    const episode = Number(url.searchParams.get('episode') || 0) || undefined;
    const budget = Math.min(Number(url.searchParams.get('budget') || 45000), 90000);

    if (!title) {
      json(res, 400, { error: 'title is required' });
      return;
    }
    if (!SITES.length) {
      json(res, 200, { streams: [], ms: 0, error: 'no sites configured' });
      return;
    }

    const out = await scrapeAll({ title, year, type, season, episode }, budget);
    json(res, 200, {
      title, year, type, total: out.streams.length, streams: out.streams, ms: out.ms,
      sites: Object.fromEntries([...lastResult.entries()])
    });
    return;
  }

  // Debug aid for keeping the drivers alive as these sites change their
  // markup. Restricted to hosts already configured as sites, so it is not an
  // open proxy.
  if (url.pathname === '/api/inspect') {
    const target = url.searchParams.get('url') || '';
    if (!inFamily(target)) {
      json(res, 403, { error: 'host is not within a configured site family' });
      return;
    }
    const r = await fetchText(target, { timeoutMs: 25000, maxBytes: 300 * 1024 });
    const title = ((r.text || '').match(/<title[^>]*>([^<]{0,140})/i) || [])[1] || '';
    // `grep` scans the WHOLE page rather than the first 40 anchors. Search
    // results sit far below a very large nav block, so the capped list shows
    // only menus and makes a working page look empty.
    const grep = (url.searchParams.get('grep') || '').toLowerCase();
    const anchors = [];
    const re = /<a\b[^>]*href\s*=\s*["']([^"']+)["'][^>]*>([\s\S]*?)<\/a>/gi;
    let m;
    while ((m = re.exec(r.text || ''))) {
      const href = m[1];
      const text = m[2].replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
      if (grep && !`${href} ${text}`.toLowerCase().includes(grep)) continue;
      anchors.push({ href: href.slice(0, 120), text: text.slice(0, 80) });
      if (anchors.length >= (grep ? 25 : 40)) break;
    }
    // Forms matter here: several of these sites search via a GET form whose
    // action and input names are the only way to build the search URL.
    const forms = [];
    const fre = /<form\b([^>]*)>([\s\S]*?)<\/form>/gi;
    let fm;
    while ((fm = fre.exec(r.text || '')) && forms.length < 10) {
      const attrs = fm[1] || '';
      const inputs = [];
      const ire = /<(?:input|select)\b([^>]*)>/gi;
      let im;
      while ((im = ire.exec(fm[2] || '')) && inputs.length < 12) {
        const a = im[1] || '';
        inputs.push({
          name: ((a.match(/name\s*=\s*["']([^"']*)["']/i) || [])[1]) || '',
          type: ((a.match(/type\s*=\s*["']([^"']*)["']/i) || [])[1]) || '',
          id: ((a.match(/id\s*=\s*["']([^"']*)["']/i) || [])[1]) || ''
        });
      }
      forms.push({
        action: ((attrs.match(/action\s*=\s*["']([^"']*)["']/i) || [])[1]) || '',
        method: ((attrs.match(/method\s*=\s*["']([^"']*)["']/i) || [])[1]) || 'get',
        role: ((attrs.match(/role\s*=\s*["']([^"']*)["']/i) || [])[1]) || '',
        inputs
      });
    }
    // Magnets are reported explicitly: they are a different shape from the
    // anchor list (no http url), and they are easy to miss when a page is
    // truncated at the anchor cap.
    const magnets = findMagnets(r.text || '').map((m) => ({
      infoHash: m.infoHash,
      label: m.label.slice(0, 80),
      trackers: (m.magnet.match(/[?&]tr=([^&]+)/g) || []).length
    }));
    const magnetAnchors = ((r.text || '').match(/href\s*=\s*["']magnet:/gi) || []).length;
    // `raw=<term>` returns text around each match. Some of these sites inject
    // results with JS or embed them in a data blob rather than in anchors, so
    // an anchor listing can show nothing while the page clearly has results.
    const rawTerm = (url.searchParams.get('raw') || '').toLowerCase();
    let raw = [];
    if (rawTerm) {
      const hay = (r.text || '').toLowerCase();
      let idx = hay.indexOf(rawTerm);
      while (idx !== -1 && raw.length < 12) {
        raw.push((r.text || '').slice(Math.max(0, idx - 120), idx + 200).replace(/\s+/g, ' '));
        idx = hay.indexOf(rawTerm, idx + rawTerm.length);
      }
    }
    json(res, 200, {
      ok: r.ok, status: r.status, finalUrl: r.url, bytes: r.bytes,
      error: r.error || null, title, forms, anchors,
      magnetAnchors, magnets, raw
    });
    return;
  }

  // Verify a scraped link really is a playable file, from the region that can
  // reach it. Streamed and never read, so a host that ignores Range cannot
  // make us pull a whole film. Restricted to hosts the config already names.
  // Unwrap an embed page into a raw media URL. Lives here rather than in the
  // main backend for the same reason the scrapers do: reachability is
  // region-dependent, and these hosts are ISP-blocked from the dev machine.
  if (url.pathname === '/api/extract') {
    const target = url.searchParams.get('url') || '';
    const referer = url.searchParams.get('referer') || '';
    const result = await extractMedia(target, { timeoutMs: 20000, referer });
    if (result.ok && result.kind === 'hls' && !result.relayUrl) {
      result.relayUrl = relayPathFor(result.mediaUrl, referer);
    }
    lastExtract.set(hostOf(target) || target, { ...result, at: Date.now() });
    json(res, result.ok ? 200 : 502, result);
    return;
  }

  if (url.pathname === '/api/hls') {
    const target = verifyToken(url.searchParams.get('t') || '');
    if (!target) {
      json(res, 403, { error: 'bad or missing token' });
      return;
    }
    const headers = { 'User-Agent': 'Mozilla/5.0' };
    const range = req.headers.range;
    if (range) headers.Range = range;
    const referer = url.searchParams.get('ref');
    if (referer) headers.Referer = referer;
    try {
      const upstream = await fetch(target, { headers });
      const ct = upstream.headers.get('content-type') || '';
      if (PLAYLIST_TYPES.test(ct)) {
        const text = await upstream.text();
        const body = rewritePlaylist(text, target);
        res.writeHead(upstream.status === 206 ? 200 : upstream.status, {
          'Content-Type': 'application/vnd.apple.mpegurl',
          'Access-Control-Allow-Origin': '*',
          'Cache-Control': 'no-store',
        });
        res.end(body);
        return;
      }
      const pass = { 'Access-Control-Allow-Origin': '*', 'Cache-Control': 'no-store' };
      for (const h of ['content-type', 'content-length', 'content-range', 'accept-ranges']) {
        const v = upstream.headers.get(h);
        if (v) pass[h] = v;
      }
      res.writeHead(upstream.status, pass);
      if (!upstream.body) {
        res.end();
        return;
      }
      const reader = upstream.body.getReader();
      for (;;) {
        // eslint-disable-next-line no-await-in-loop
        const { done, value } = await reader.read();
        if (done) break;
        res.write(Buffer.from(value));
      }
      res.end();
    } catch (err) {
      json(res, 502, { error: `relay: ${String((err && err.message) || err).slice(0, 140)}` });
    }
    return;
  }

  if (url.pathname === '/api/probe') {
    const target = url.searchParams.get('url') || '';
    if (!inFamily(target)) {
      json(res, 403, { error: 'host is not within a configured site family' });
      return;
    }
    if (!/^https?:/i.test(target)) {
      json(res, 400, { error: 'url must be http(s)' });
      return;
    }
    try {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 20000);
      const resp = await fetch(target, {
        headers: { Range: 'bytes=0-2047', 'User-Agent': 'Mozilla/5.0' },
        signal: ctrl.signal
      });
      clearTimeout(timer);
      const ct = (resp.headers.get('content-type') || '').split(';')[0];
      const out = {
        status: resp.status,
        contentType: ct,
        contentRange: resp.headers.get('content-range'),
        contentLength: resp.headers.get('content-length'),
        acceptRanges: resp.headers.get('accept-ranges'),
        disposition: (resp.headers.get('content-disposition') || '').slice(0, 120)
      };
      try {
        await resp.body?.cancel();
      } catch {
        /* nothing to release */
      }
      json(res, 200, out);
    } catch (err) {
      json(res, 200, { status: 0, error: String(err.message || err) });
    }
    return;
  }

  if (url.pathname === '/api/resolve') {
    const target = url.searchParams.get('url') || '';
    const result = await resolveHost(target, { timeoutMs: 20000 });
    json(res, result.ok ? 200 : 502, result);
    return;
  }

  json(res, 404, { error: 'not found', endpoints: ['/health', '/api/scrape', '/api/resolve', '/api/inspect', '/api/probe', '/api/extract', '/api/hls'] });
}

server.listen(PORT, '0.0.0.0', () => {
  console.log(`[scrapers] listening on :${PORT}`);
  console.log(`[scrapers] sites: ${SITES.map((s) => s.name).join(', ') || '(none configured)'}`);
});
