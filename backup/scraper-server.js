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
const fs = require('fs');
const path = require('path');

const { runSite, isSiteUpstream } = require('./lib/sites');
const { fetchText } = require('./lib/util');
const { resolveHost } = require('./lib/resolvers');

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
const startedAt = Date.now();
const lastResult = new Map(); // site name -> { ok, count, ms, error, at }

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
  const seen = new Set();
  const streams = [];
  for (const list of results) {
    for (const s of list) {
      const key = (s.url || '').split('?')[0];
      if (!key || seen.has(key)) continue;
      seen.add(key);
      streams.push(s);
    }
  }
  return { streams, ms: Date.now() - started };
}

const server = http.createServer(async (req, res) => {
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
    json(res, 200, { title, year, type, total: out.streams.length, streams: out.streams, ms: out.ms });
    return;
  }

  // Debug aid for keeping the drivers alive as these sites change their
  // markup. Restricted to hosts already configured as sites, so it is not an
  // open proxy.
  if (url.pathname === '/api/inspect') {
    const target = url.searchParams.get('url') || '';
    const allowed = SITES.some((s) =>
      (s.domains || []).some((d) => {
        const host = String(d).replace(/^https?:\/\//, '').replace(/\/.*$/, '');
        return target.includes(host);
      })
    );
    if (!allowed) {
      json(res, 403, { error: 'host is not one of the configured sites' });
      return;
    }
    const r = await fetchText(target, { timeoutMs: 25000, maxBytes: 300 * 1024 });
    const title = ((r.text || '').match(/<title[^>]*>([^<]{0,140})/i) || [])[1] || '';
    const anchors = [];
    const re = /<a\b[^>]*href\s*=\s*["']([^"']+)["'][^>]*>([\s\S]*?)<\/a>/gi;
    let m;
    while ((m = re.exec(r.text || '')) && anchors.length < 40) {
      anchors.push({
        href: m[1].slice(0, 110),
        text: m[2].replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 70)
      });
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
    json(res, 200, {
      ok: r.ok, status: r.status, finalUrl: r.url, bytes: r.bytes,
      error: r.error || null, title, forms, anchors
    });
    return;
  }

  // Verify a scraped link really is a playable file, from the region that can
  // reach it. Streamed and never read, so a host that ignores Range cannot
  // make us pull a whole film. Restricted to hosts the config already names.
  if (url.pathname === '/api/probe') {
    const target = url.searchParams.get('url') || '';
    const named = SITES.some((s) =>
      [...(s.domains || []), ...(s.linkHosts || [])].some((n) =>
        target.includes(String(n).replace(/^https?:\/\//, '').replace(/\/.*$/, ''))
      )
    );
    if (!named) {
      json(res, 403, { error: 'host is not named by any configured site' });
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

  json(res, 404, { error: 'not found', endpoints: ['/health', '/api/scrape', '/api/resolve', '/api/inspect', '/api/probe'] });
});

server.listen(PORT, '0.0.0.0', () => {
  console.log(`[scrapers] listening on :${PORT}`);
  console.log(`[scrapers] sites: ${SITES.map((s) => s.name).join(', ') || '(none configured)'}`);
});
