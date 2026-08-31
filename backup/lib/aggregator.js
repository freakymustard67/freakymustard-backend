'use strict';
/**
 * freaky-backup · aggregator.js
 * Merges results from multiple upstream Stremio addons into one clean,
 * deduplicated, sensibly-ordered stream list.
 */

const { qualityScore } = require('./util');

function streamKey(s) {
  if (s.infoHash) return `ih:${String(s.infoHash).toLowerCase()}:${s.fileIdx ?? 'x'}`;
  if (s.externalUrl) return `ext:${s.externalUrl}`;
  if (s.ytId) return `yt:${s.ytId}`;
  return `url:${s.url}`;
}

function isInfoHashStream(s) {
  return Boolean(s.infoHash) && !s.url;
}

/**
 * @param {Array<{upstream:object, result:{ok:boolean, streams:Array, error?:string, ms:number}}>} results
 */
function aggregate(results) {
  const streams = [];
  const seen = new Set();
  const sources = [];
  const upstreamTotal = results.reduce(
    (n, r) => n + ((r && r.result && Array.isArray(r.result.streams)) ? r.result.streams.length : 0),
    0
  );

  for (const { upstream, result } of results) {
    sources.push({
      upstream: upstream.name,
      tag: upstream.tag,
      ok: result.ok,
      count: result.streams.length,
      ms: result.ms,
      error: result.error || null
    });
    for (const s of result.streams) {
      const key = streamKey(s);
      if (seen.has(key)) continue;
      seen.add(key);

      const out = { ...s };
      // Tag the source so users can see where each link came from.
      const origName = (out.name || '').replace(/\n+/g, ' · ');
      out.name = `[${upstream.tag}] ${origName}`.trim();
      if (!out.title && origName) out.title = origName;
      out.freaky = {
        source: upstream.name,
        kind: isInfoHashStream(out) ? 'torrent' : 'direct'
      };
      streams.push(out);
    }
  }

  // Order: direct-playable first, then by rough quality score.
  streams.sort((a, b) => {
    const directDiff = Number(Boolean(b.url)) - Number(Boolean(a.url));
    if (directDiff) return directDiff;
    return qualityScore(b) - qualityScore(a);
  });

  return {
    streams,
    sources,
    meta: {
      total: streams.length,
      direct: streams.filter((s) => s.url).length,
      torrent: streams.filter(isInfoHashStream).length,
      upstreamTotal,
      deduped: upstreamTotal - seen.size
    }
  };
}

module.exports = { aggregate };
