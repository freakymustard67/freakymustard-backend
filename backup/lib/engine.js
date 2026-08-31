'use strict';
/**
 * freaky-backup · engine.js
 * Optional torrent-to-direct-HTTP-stream engine powered by webtorrent.
 * Lazily imported: the rest of the server works fine without it.
 *
 * Given an infoHash (+ optional file index), it downloads that file via P2P
 * and serves its bytes over HTTP with full Range support so any player or
 * <video> tag can stream it directly — no Stremio client needed.
 */

const crypto = require('crypto');
const path = require('path');

const TRACKERS = [
  'wss://tracker.btorrent.xyz',
  'wss://tracker.fastcast.nz',
  'wss://tracker.openwebtorrent.com',
  'udp://tracker.opentrackr.org:1337',
  'udp://explodie.org:6969',
  'udp://tracker.torrent.eu.org:451',
  'udp://open.demonii.com:1337',
  'udp://tracker.tiny-vps.com:6969'
];

const MIME = {
  '.mp4': 'video/mp4',
  '.m4v': 'video/mp4',
  '.mkv': 'video/x-matroska',
  '.webm': 'video/webm',
  '.avi': 'video/x-msvideo',
  '.mov': 'video/quicktime',
  '.ts': 'video/mp2t'
};

class TorrentEngine {
  /**
   * @param {{maxTorrents?:number, idleEvictMs?:number, log?:function}} opts
   */
  constructor({ maxTorrents = 3, idleEvictMs = 15 * 60 * 1000, log = () => {} } = {}) {
    this.maxTorrents = maxTorrents;
    this.idleEvictMs = idleEvictMs;
    this.log = log;
    this.client = null;
    this.entries = new Map(); // infoHash(lower) -> {torrent, file, fileIdx, lastAccess, ready}
    this.allowlist = new Set(); // infoHashes recently returned by aggregation
    this.evictTimer = setInterval(() => this._evictIdle(), Math.min(60_000, idleEvictMs));
    if (this.evictTimer.unref) this.evictTimer.unref();
    this.stats = { requests: 0, bytesServed: 0, errors: 0 };
  }

  async _ensureClient() {
    if (this.client) return this.client;
    const mod = await import('webtorrent');
    const WebTorrent = mod.default || mod;
    this.client = new WebTorrent();
    this.client.on('error', (e) => this.log('engine client error:', e.message));
    return this.client;
  }

  /** Mark an infoHash as servable (called by the aggregator path). */
  allow(infoHash) {
    if (infoHash) this.allowlist.add(String(infoHash).toLowerCase());
    // keep the set bounded
    if (this.allowlist.size > 5000) {
      this.allowlist = new Set([...this.allowlist].slice(-2500));
    }
  }

  isAllowed(infoHash) {
    return this.allowlist.has(String(infoHash || '').toLowerCase());
  }

  /** Get or attach a torrent + pick the target file. Concurrent callers share one fetch. */
  async ensure(infoHash, wantedIdx) {
    const key = String(infoHash).toLowerCase();
    let entry = this.entries.get(key);
    if (entry && entry.ready) {
      entry.lastAccess = Date.now();
      return entry;
    }

    if (!entry) {
      const client = await this._ensureClient();
      // NOTE: pass an OBJECT (not a magnet string) — avoids magnet re-parse issues.
      const torrentId = { infoHash: key, announce: [...TRACKERS] };
      entry = { torrent: null, file: null, fileIdx: null, lastAccess: Date.now(), ready: false, pending: null };
      this.entries.set(key, entry);

      // LRU cap
      const active = [...this.entries.values()].filter((e) => e.torrent);
      while (active.length >= this.maxTorrents) {
        active.sort((a, b) => a.lastAccess - b.lastAccess);
        const oldest = active.shift();
        this.log('engine evicting torrent for capacity:', oldest.file && oldest.file.name);
        try { this.client.remove(oldest.torrent); } catch { /* noop */ }
      }

      entry.pending = new Promise((resolve, reject) => {
        const to = setTimeout(() => reject(new Error('metadata timeout (no peers/seeders reachable?)')), 45000);
        const fail = (e) => {
          clearTimeout(to);
          this.entries.delete(key); // allow a clean retry later
          reject(e);
        };
        try {
          client.add(torrentId, (torrent) => { clearTimeout(to); resolve(torrent); });
          client.once('error', fail);
        } catch (e) {
          fail(e);
        }
      });
      try {
        entry.torrent = await entry.pending;
      } finally {
        entry.pending = null;
      }
      entry.torrent.on('done', () => this.log('engine download complete:', entry.torrent.name));
      this.log(`engine got metadata: ${entry.torrent.name} (${entry.torrent.files.length} files)`);
    } else if (entry.pending) {
      // another request is already fetching metadata for this hash — join it
      await entry.pending;
    } else if (!entry.ready) {
      throw new Error('torrent attach failed; retry');
    }

    // pick file
    const files = entry.torrent.files || [];
    if (!files.length) throw new Error('torrent has no files');
    let idx = Number.isFinite(wantedIdx) ? wantedIdx : -1;
    if (idx < 0 || !files[idx]) {
      // largest video-ish file
      let best = -1, bestLen = -1;
      files.forEach((f, i) => {
        if (MIME[path.extname(f.name).toLowerCase()] && f.length > bestLen) { best = i; bestLen = f.length; }
      });
      idx = best >= 0 ? best : 0;
    }
    const file = files[idx];
    file.select();
    entry.file = file;
    entry.fileIdx = idx;
    entry.ready = true;
    entry.lastAccess = Date.now();
    return entry;
  }

  _evictIdle() {
    const now = Date.now();
    for (const [hash, entry] of this.entries) {
      if (now - entry.lastAccess > this.idleEvictMs) {
        this.log('engine evicting idle torrent:', entry.file && entry.file.name);
        try { this.client && this.client.remove(entry.torrent); } catch { /* noop */ }
        this.entries.delete(hash);
      }
    }
  }

  contentType(name) {
    return MIME[path.extname(name || '').toLowerCase()] || 'application/octet-stream';
  }

  status() {
    return {
      running: Boolean(this.client),
      activeTorrents: this.entries.size,
      maxTorrents: this.maxTorrents,
      allowedHashes: this.allowlist.size,
      ...this.stats
    };
  }

  async destroy() {
    clearInterval(this.evictTimer);
    if (this.client) {
      try { await this.client.destroy(); } catch { /* noop */ }
    }
  }
}

/**
 * Handle an engine request. Path shape: /d/<40-hex-infoHash>[/<fileIdx>]/<anything>
 * Returns true if handled.
 */
async function handleEngineRequest(engine, pathname, searchParams, req, res, log) {
  const m = /^\/d\/([0-9a-fA-F]{40})(?:\/(\d+))?(?:\/.*)?$/.exec(pathname);
  if (!m) return false;

  /** Pipe torrent file bytes to res, tolerating client aborts mid-stream. */
  const pipeFile = (file, opts) => {
    const rs = file.createReadStream(opts);
    rs.on('error', (e) => {
      log('engine read error:', e.message);
      try { res.destroy(); } catch { /* noop */ }
    });
    res.on('close', () => { try { rs.destroy(); } catch { /* noop */ } });
    rs.pipe(res);
  };

  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Accept-Ranges', 'bytes');

  const infoHash = m[1];
  const fileIdx = m[2] !== undefined ? parseInt(m[2], 10) : undefined;

  if (!engine.isAllowed(infoHash)) {
    res.writeHead(404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'unknown infoHash (not offered by this server)' }));
    return true;
  }
  if (req.method === 'HEAD') {
    res.writeHead(200);
    res.end();
    return true;
  }

  engine.stats.requests += 1;
  let entry;
  try {
    entry = await engine.ensure(infoHash, fileIdx);
  } catch (err) {
    engine.stats.errors += 1;
    log('engine error:', err.message);
    res.writeHead(502, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'torrent unavailable', detail: err.message }));
    return true;
  }

  const file = entry.file;
  const total = file.length;
  const type = engine.contentType(file.name);
  res.setHeader('Content-Type', type);
  const wantDownload = searchParams.get('download') === '1' || searchParams.get('attachment') === '1';
  res.setHeader('Content-Disposition', `${wantDownload ? 'attachment' : 'inline'}; filename="${file.name.replace(/"/g, '')}"`);
  res.setHeader('X-Stream-File', file.name);

  const range = req.headers.range;
  if (range) {
    const rm = /^bytes=(\d*)-(\d*)$/.exec(range.trim());
    let start = rm && rm[1] ? parseInt(rm[1], 10) : 0;
    let end = rm && rm[2] ? parseInt(rm[2], 10) : total - 1;
    if (Number.isNaN(start) || Number.isNaN(end) || start > end || start >= total) {
      res.writeHead(416, { 'Content-Range': `bytes */${total}` });
      res.end();
      return true;
    }
    end = Math.min(end, total - 1);
    res.writeHead(206, {
      'Content-Range': `bytes ${start}-${end}/${total}`,
      'Content-Length': end - start + 1
    });
    engine.stats.bytesServed += end - start + 1;
    if (req.method === 'GET') pipeFile(file, { start, end });
    else res.end();
    return true;
  }

  res.writeHead(200, { 'Content-Length': total });
  engine.stats.bytesServed += total;
  if (req.method === 'GET') pipeFile(file, {});
  else res.end();
  return true;
}

module.exports = { TorrentEngine, handleEngineRequest };
