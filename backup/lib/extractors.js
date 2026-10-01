'use strict';
/**
 * freaky-backup · extractors.js
 *
 * Turn an ad-laden embed URL into a raw media URL that we can play ourselves.
 *
 * The Tamil/Indian sites never host video. Every "Watch Online" button points
 * at a third-party player whose page is mostly advertising, and whose real
 * stream is hidden behind one of a few repeated tricks:
 *
 *   - a Dean Edwards packed `eval(...)` containing a JWPlayer config
 *     (Streamwish / Filelions / StreamHG and their rotating domains)
 *   - a plain `file:` / `sources:` assignment (Streamhub / VCDN, and others)
 *   - a small API hop keyed by an id in the page (Netu / hqq.ac)
 *
 * So the general strategy is: fetch the embed page, look for a media URL,
 * unpack anything packed, look again. Host-specific behaviour is a small hook
 * on top of that, not a separate implementation per host.
 *
 * Nothing here executes fetched JavaScript — only string work (see unpack.js).
 * The output is a URL plus the headers it needs, which the caller hands to the
 * signed /hls proxy so the browser never talks to the ad-laden origin and the
 * CDN never sees a browser Referer.
 */

const { fetchText } = require('./util');
const { extractMediaUrls } = require('./unpack');

/**
 * Host registry. `match` is tested against the embed URL; the first hit wins.
 * `kind` selects the strategy:
 *   scan   — fetch and look for a media URL (unpacking if needed)
 *   netu   — resolve an id through Netu's own API
 *   skip   — known to be unroutable without a browser; reported honestly
 */
const HOSTS = [
  { id: 'netu', match: /hqq\.(ac|to|nu|watch)|netu\.|waaw\./i, kind: 'netu' },
  { id: 'streamwish', match: /streamwish|niramirus|streamhg|embedwis|swish|hanerix|strwish/i, kind: 'scan' },
  { id: 'filelions', match: /filelions|callistanise|filemoon|vidhide|dhcplay|dinisgl/i, kind: 'scan' },
  { id: 'streamhub', match: /vcdnlare|streamhub|vcdn|streamhide|hubstream/i, kind: 'scan' },
  { id: 'streamtape', match: /tapepops|streamtape|tapecontent/i, kind: 'scan' },
  { id: 'uperbox', match: /uperbox/i, kind: 'scan' },
  { id: 'hgcloud', match: /hgcloud/i, kind: 'scan' },
  { id: 'gofile', match: /gofile\.io/i, kind: 'skip', note: 'gofile now requires an X-Website-Token' },
  { id: 'easysyncr', match: /easysyncr/i, kind: 'skip', note: 'url shortener, no media behind it' },
];

function classify(embedUrl) {
  const url = String(embedUrl || '');
  for (const host of HOSTS) if (host.match.test(url)) return host;
  return { id: 'unknown', kind: 'scan' };
}

/** Absolute iframe targets on a page (the wrapper players are just an iframe). */
function findIframes(html, base) {
  const out = [];
  const re = /<iframe\b[^>]*src\s*=\s*["']([^"']+)["']/gi;
  let m;
  while ((m = re.exec(html))) {
    let src = m[1].replace(/&amp;/g, '&').trim();
    if (!src || /^(about:|javascript:|data:)/i.test(src)) continue;
    if (src.startsWith('//')) src = `https:${src}`;
    else if (!/^https?:/i.test(src)) {
      try {
        src = new URL(src, base).toString();
      } catch {
        continue;
      }
    }
    if (!out.includes(src)) out.push(src);
  }
  return out;
}

/** Prefer an HLS master over a media file, and a master over a variant. */
function hlsScore(u) {
  let score = 0;
  if (/master|manifest|playlist/i.test(u)) score += 10;
  // A resolution in the path is the signature of a variant playlist, e.g.
  // /hls/<id>/720/index.m3u8 — and that path also contains "index", so a plain
  // keyword search picks the variant over the master.
  if (/\b(\d{3,4})p\b|\/\d{3,4}\//.test(u)) score -= 6;
  score -= (u.split('/').length - 3) * 0.5; // shallower paths are usually masters
  return score;
}

function pickBest(urls) {
  const hls = urls.filter((u) => /\.m3u8(\?|$)/i.test(u));
  if (hls.length) {
    const master = hls.slice().sort((a, b) => hlsScore(b) - hlsScore(a))[0];
    return { mediaUrl: master, kind: 'hls' };
  }
  const mp4 = urls.filter((u) => /\.(mp4|m4v|webm)(\?|$)/i.test(u));
  if (mp4.length) return { mediaUrl: mp4[0], kind: 'file' };
  const mpd = urls.filter((u) => /\.mpd(\?|$)/i.test(u));
  if (mpd.length) return { mediaUrl: mpd[0], kind: 'dash' };
  return null;
}

/**
 * Netu (hqq.ac) keeps the real playlist behind its own endpoint. The embed
 * page carries an id and a `ws` query fragment used to ask the API.
 */
async function extractNetu(embedUrl, ctx) {
  const page = await fetchText(embedUrl, { timeoutMs: ctx.timeoutMs, headers: ctx.headers });
  if (!page.ok) return { ok: false, error: `netu page: ${page.error}` };

  const scan = extractMediaUrls(page.text);
  if (!scan.fromCommentsOnly) {
    const direct = pickBest(scan.urls);
    if (direct) return { ok: true, ...direct, evidence: 'netu: media url in page' };
  }

  const id =
    (embedUrl.match(/\/e\/([A-Za-z0-9_-]+)/) || [])[1] ||
    (page.text.match(/og:url["'][^>]*\/f\/([A-Za-z0-9_-]+)/i) || [])[1] ||
    (page.text.match(/[?&]id=([A-Za-z0-9_-]{6,})/) || [])[1];
  if (!id) return { ok: false, error: 'netu: no media id found' };

  // The player loads /dl?op=…&id=…&f=… style endpoints; try the documented
  // ones and accept the first that hands back a playlist.
  const origin = new URL(embedUrl).origin;
  const tries = [
    `${origin}/dl?op=view&id=${id}`,
    `${origin}/dl?op=embed&id=${id}`,
    `${origin}/e/${id}`,
  ];
  for (const url of tries) {
    // eslint-disable-next-line no-await-in-loop
    const res = await fetchText(url, { timeoutMs: ctx.timeoutMs, headers: { ...ctx.headers, Referer: embedUrl } });
    if (!res.ok) continue;
    const found = pickBest(extractMediaUrls(res.text).urls);
    if (found) return { ok: true, ...found, evidence: `netu: ${url}` };
  }
  return { ok: false, error: 'netu: id found but no media url returned (needs its JS handshake)' };
}

/**
 * Resolve an embed URL to a raw media URL.
 *
 * @param {string} embedUrl
 * @param {{timeoutMs?:number, referer?:string}} [opts]
 * @returns {Promise<{ok:boolean, mediaUrl?:string, kind?:string, host?:string,
 *                    headers?:object, evidence?:string, error?:string}>}
 */
async function extractMedia(embedUrl, opts = {}) {
  const timeoutMs = opts.timeoutMs || 15000;
  if (!/^https?:\/\//i.test(String(embedUrl || ''))) {
    return { ok: false, error: 'unsafe or missing embed url' };
  }
  const host = classify(embedUrl);
  // Hosts sometimes require the referring site (or refuse a browser-looking
  // one). We send the caller's referer, never a full browser fingerprint.
  const headers = opts.referer ? { Referer: opts.referer } : {};
  const ctx = { timeoutMs, headers };

  if (host.kind === 'skip') {
    return { ok: false, host: host.id, error: `skipped: ${host.note || 'not extractable'}` };
  }

  try {
    if (host.kind === 'netu') {
      const res = await extractNetu(embedUrl, ctx);
      return { ...res, host: host.id, headers };
    }

    // Wrapper players (the site's own /waaw/?l=…, and several hosts) are just a
    // shell around an iframe pointing at the real player, so follow one level.
    let current = embedUrl;
    let referer = opts.referer || '';
    let commentFallback = null;
    for (let depth = 0; depth < 3; depth += 1) {
      // Re-classify at every hop. A wrapper on the movie site is "unknown",
      // but the iframe it points at is a Netu player with its own strategy —
      // scanning that page generically picks up decoy URLs (an old preview
      // playlist) instead of the real stream.
      const hopHost = classify(current);
      if (hopHost.kind === 'netu' && depth > 0) {
        const res = await extractNetu(current, ctx);
        if (res.ok) return { ...res, host: hopHost.id, headers, via: current, evidence: `netu at depth ${depth}` };
        if (depth === 2) return { ...res, host: hopHost.id, headers, via: current };
      }
      // eslint-disable-next-line no-await-in-loop
      const page = await fetchText(current, {
        timeoutMs,
        headers: referer ? { ...headers, Referer: referer } : headers,
      });
      if (!page.ok) return { ok: false, host: host.id, error: `embed page: ${page.error} (${current.slice(0, 60)})` };

      const { urls, usedUnpack, unpacked, fromCommentsOnly } = extractMediaUrls(page.text);
      const best = fromCommentsOnly ? null : pickBest(urls);
      if (best) {
        return {
          ok: true,
          ...best,
          host: host.id,
          headers,
          evidence: usedUnpack ? `unpacked packed config at depth ${depth}` : `media url in page at depth ${depth}`,
          via: current,
        };
      }
      // Remember a comment-only URL, but keep looking: it is usually a template.
      if (fromCommentsOnly && !commentFallback) commentFallback = pickBest(urls);

      const frames = findIframes(page.text, page.url || current).filter((f) => f !== current);
      if (!frames.length) {
        return {
          ok: false,
          host: host.id,
          error: usedUnpack
            ? 'unpacked the config but it held no media url'
            : 'no media url found (nothing packed either)',
          unpackedSample: usedUnpack ? String(unpacked).replace(/\s+/g, ' ').slice(0, 400) : undefined,
        };
      }
      referer = page.url || current;
      current = frames[0];
    }
    if (commentFallback) {
      return {
        ok: true,
        ...commentFallback,
        host: host.id,
        headers,
        evidence: 'only a commented-out template URL exists — likely not the real stream',
        fromCommentsOnly: true,
      };
    }
    return { ok: false, host: host.id, error: 'followed iframes but never reached a media url' };
  } catch (err) {
    return { ok: false, host: host.id, error: String((err && err.message) || err).slice(0, 160) };
  }
}

module.exports = { extractMedia, classify, pickBest, hlsScore, findIframes, HOSTS };
