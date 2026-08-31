'use strict';
/**
 * freaky-backup · app.js
 * HTTP application factory. One instance = one config = one server.
 * English (:8101) and Tamil (:8102) run as fully separate processes.
 */

const http = require('http');
const { URL } = require('url');

const { TTLCache } = require('./cache');
const { log, fetchJson, isSafeHttpUrl, validateId, qualityScore, UA } = require('./util');
const { fetchStreams, fetchManifest } = require('./upstream');
const { aggregate } = require('./aggregator');
const { landingPage } = require('./landing');

function createApp(config) {
  const cfg = config;
  const instance = cfg.instance || 'default';
  const cache = new TTLCache({ ttlMs: (cfg.cacheTtlSeconds || 600) * 1000, name: `${instance}-streams` });
  const counters = { requests: 0, streamRequests: 0, cacheHits: 0, errors: 0 };
  const startedAt = Date.now();
  const upstreamStatus = new Map(); // name -> last probe result

  // ---- optional torrent→direct-stream engine -------------------------------
  let engine = null;
  if (cfg.engine && cfg.engine.enabled) {
    try {
      const { TorrentEngine } = require('./engine');
      engine = new TorrentEngine({
        maxTorrents: cfg.engine.maxTorrents || 3,
        idleEvictMs: cfg.engine.idleEvictMs || 15 * 60 * 1000,
        log: (...a) => log(instance, ...a)
      });
    } catch (e) {
      log(instance, 'engine disabled:', e.message);
    }
  }

  // ---- core stream resolution ----------------------------------------------
  async function resolveStreams(type, id) {
    const key = `${type}:${id}`;
    const cached = cache.get(key);
    if (cached) {
      counters.cacheHits += 1;
      return cached;
    }
    const timeoutMs = cfg.upstreamTimeoutMs || 12000;
    // Hard response deadline: answer with whatever arrived in time; late
    // upstream results get folded into the cache asynchronously afterwards.
    const deadlineMs = cfg.responseDeadlineMs || Math.min(timeoutMs + 4000, 18000);
    const startedAt = Date.now();
    const active = cfg.upstreams.filter((u) => !u.types || u.types.includes(type));

    const collected = new Map(); // upstream.name -> { upstream, result }
    let finished = 0;

    const record = (upstream, result) => {
      collected.set(upstream.name, { upstream, result });
      upstreamStatus.set(upstream.name, {
        ok: result.ok,
        count: result.streams.length,
        ms: result.ms,
        error: result.error || null,
        at: Date.now()
      });
    };

    const runOne = async (upstream) => {
      try {
        let result = await fetchStreams(upstream, type, id, timeoutMs);
        record(upstream, result);
        // one gentle retry on failure — but only while time budget remains
        if (!result.ok && !upstream.optional && Date.now() < startedAt + deadlineMs - 3000) {
          const left = startedAt + deadlineMs - Date.now();
          result = await fetchStreams(upstream, type, id, Math.max(2000, Math.min(left - 1000, 25000)));
          record(upstream, result);
        }
      } catch { /* recorded as absent */ } finally {
        finished += 1;
      }
    };

    const work = active.map((u) => runOne(u));
    await Promise.race([
      Promise.all(work),
      new Promise((r) => setTimeout(r, deadlineMs))
    ]);

    const merged = aggregate([...collected.values()]);

    if (engine) {
      for (const s of merged.streams) {
        if (s.infoHash) engine.allow(s.infoHash);
        else if (/\/([0-9a-f]{40})\b/i.test(`${s.title} ${s.name}`)) {
          engine.allow(RegExp.$1);
        }
      }
    }

    // Full TTL for real results; tiny TTL for empty ones so an upstream blip
    // never blanks a title for the whole cache window.
    cache.set(key, merged, merged.streams.length ? cache.ttlMs : 45000);

    if (finished < active.length) {
      // fold in stragglers when they land so the NEXT request gets everything
      Promise.all(work).then(() => {
        const full = aggregate([...collected.values()]);
        if (full.streams.length > merged.streams.length) {
          cache.set(key, full, cache.ttlMs);
          log(instance, `late upstream results folded into cache for ${type}/${id} → ${full.streams.length}`);
        }
      }).catch(() => {});
    }

    return merged;
  }

  function enginePublicBase(req) {
    if (cfg.publicBaseUrl) return cfg.publicBaseUrl.replace(/\/+$/, '');
    const proto = req.headers['x-forwarded-proto'] || 'http';
    const host = req.headers.host || `127.0.0.1:${cfg.port}`;
    return `${proto}://${host}`;
  }

  /** Add direct-HTTP url variants for torrent streams when the engine is on. */
  async function decorateWithEngine(streams, req) {
    if (!engine) return streams;
    const base = enginePublicBase(req);
    const extra = [];
    for (const s of streams) {
      if (!s.infoHash) continue;
      const ih = String(s.infoHash).toLowerCase();
      const idx = Number.isInteger(s.fileIdx) ? s.fileIdx : undefined;
      const safeName = encodeURIComponent((s.behaviorHints && s.behaviorHints.filename) || 'stream.mp4');
      const url = idx === undefined ? `${base}/d/${ih}/0/${safeName}` : `${base}/d/${ih}/${idx}/${safeName}`;
      extra.push({
        ...s,
        url,
        infoHash: undefined,
        fileIdx: undefined,
        name: `${(s.name || '').split(']')[0]}] ⚡direct`,
        behaviorHints: { ...(s.behaviorHints || {}), notWebReady: false },
        freaky: { source: s.freaky && s.freaky.source, kind: 'direct-engine' }
      });
    }
    // keep original torrent entries too; put engine variants first among directs
    return [...extra, ...streams];
  }

  // ---- manifest --------------------------------------------------------------
  function manifest(origin) {
    return {
      id: cfg.addonId,
      version: cfg.version,
      name: cfg.displayName,
      description: cfg.description,
      logo: `${origin}/logo.svg`,
      background: `${origin}/logo.svg`,
      types: ['movie', 'series'],
      resources: [{ name: 'stream', types: ['movie', 'series'], idPrefixes: ['tt', 'tmdb:', 'dsf:'] }],
      idPrefixes: ['tt', 'tmdb:', 'dsf:'],
      catalogs: [],
      behaviorHints: { configurable: false, adult: false },
      contactEmail: 'none@localhost'
    };
  }

  // ---- router -----------------------------------------------------------------
  async function route(req, res) {
    const started = Date.now();
    counters.requests += 1;
    const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
    const pathname = decodeURIComponent(url.pathname);
    const origin = enginePublicBase(req);

    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader('Access-Control-Allow-Headers', '*');
    res.setHeader('Access-Control-Allow-Methods', 'GET,OPTIONS');
    if (req.method === 'OPTIONS') { res.writeHead(204); res.end(); return; }

    const json = (code, obj) => {
      const body = JSON.stringify(obj, null, 2);
      res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8' });
      res.end(body);
    };

    try {
      // engine endpoints first (own guard logic)
      if (engine) {
        const handled = await require('./engine').handleEngineRequest(
          engine, pathname, url.searchParams, req, res, (...a) => log(instance, ...a)
        );
        if (handled) {
          log(instance, `${req.method} ${pathname} → ${res.statusCode} (${Date.now() - started}ms)`);
          return;
        }
      }

      switch (true) {
        case pathname === '/' || pathname === '/index.html':
          if (req.method !== 'GET') { json(405, { error: 'method not allowed' }); return; }
          res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
          res.end(landingPage(cfg, origin, upstreamStatus));
          break;

        case pathname === '/manifest.json':
          json(200, manifest(origin));
          break;

        case pathname === '/healthz': {
          const ups = {};
          for (const u of cfg.upstreams) ups[u.tag] = upstreamStatus.get(u.name) || { ok: null };
          json(200, {
            ok: true,
            instance,
            uptimeSec: Math.round((Date.now() - startedAt) / 1000),
            upstreams: ups,
            engine: engine ? engine.status() : { enabled: false }
          });
          break;
        }

        case pathname === '/stats':
          json(200, {
            instance,
            uptimeSec: Math.round((Date.now() - startedAt) / 1000),
            requests: counters,
            cache: cache.stats(),
            engine: engine ? engine.status() : { enabled: false },
            upstreamLastProbe: Object.fromEntries([...upstreamStatus.entries()])
          });
          break;

        case pathname.startsWith('/stream/') && pathname.endsWith('.json'): {
          const mm = /^\/stream\/(movie|series|anime)\/(.+)\.json$/.exec(pathname);
          if (!mm) { json(404, { error: 'unsupported stream path' }); return; }
          const [, rawType, rawId] = mm;
          const type = rawType === 'anime' ? 'series' : rawType;
          const id = validateId(type, rawId);
          if (!id) { json(400, { error: 'invalid id', hint: 'expected tt1234567 or tt1234567:s:e (or tmdb:/dsf: ids)' }); return; }

          counters.streamRequests += 1;
          const merged = await resolveStreams(type, id);
          const streams = await decorateWithEngine(merged.streams, req);
          log(instance, `streams ${type}/${id} → ${streams.length} (${Date.now() - started}ms)`);
          json(200, { streams, ...(url.searchParams.has('debug') ? { sources: merged.sources } : {}) });
          break;
        }

        case pathname === '/api/streams': {
          const type = url.searchParams.get('type') || 'movie';
          const idRaw = url.searchParams.get('id') || '';
          if (!['movie', 'series'].includes(type)) { json(400, { error: 'type must be movie|series' }); return; }
          const id = validateId(type, idRaw);
          if (!id) { json(400, { error: 'invalid id', hint: 'expected tt1234567 or tt1234567:s:e' }); return; }
          counters.streamRequests += 1;
          const merged = await resolveStreams(type, id);
          const streams = await decorateWithEngine(merged.streams, req);
          log(instance, `api ${type}/${id} → ${streams.length} (${Date.now() - started}ms)`);
          json(200, { query: { type, id }, total: streams.length, streams, sources: merged.sources });
          break;
        }

        case pathname === '/logo.svg': {
          const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="${cfg.accent}"/><stop offset="1" stop-color="${cfg.accent2}"/></linearGradient></defs>
<rect width="64" height="64" rx="14" fill="#0b0e14"/>
<path d="M20 16l28 16-28 16z" fill="url(#g)"/>
</svg>`;
          res.writeHead(200, { 'Content-Type': 'image/svg+xml' });
          res.end(svg);
          break;
        }

        default:
          json(404, { error: 'not found', endpoints: ['/manifest.json', '/stream/{type}/{id}.json', '/api/streams', '/healthz', '/stats'] });
      }
    } catch (err) {
      counters.errors += 1;
      log(instance, 'handler error:', err.stack || err.message);
      if (!res.headersSent) json(500, { error: 'internal error' });
    }
  }

  async function probeUpstreams() {
    for (const u of cfg.upstreams) {
      const r = await fetchManifest(u, 8000);
      upstreamStatus.set(u.name, {
        ok: Boolean(r.ok),
        count: null,
        ms: r.ms,
        error: r.error || null,
        addonName: r.name || u.name,
        at: Date.now()
      });
      log(instance, `probe ${u.name}: ${r.ok ? `OK "${r.name}"` : `FAIL ${r.error}`} (${r.ms}ms)`);
    }
  }

  async function shutdown() {
    if (engine) await engine.destroy();
  }

  return { handler: route, probeUpstreams, shutdown, config: cfg };
}

module.exports = { createApp };
