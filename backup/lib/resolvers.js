'use strict';
/**
 * freaky-backup · resolvers.js
 *
 * Turn a file-host *landing page* into a direct, ranged file URL.
 *
 * The Tamil sites (and the HDHub/4KHDHub stack) never link a video directly:
 * every download button points at an interstitial gate. A browser <video> can
 * never play those, which is why "page" rows look broken. These are the
 * recipes, all verified live, none of which need a headless browser:
 *
 *   pixeldrain     already a ranged video URL — pass through untouched
 *   hubdrive.pics  a gate; it just links to hubcloud.ist/drive/<id>
 *   hubcloud.ist   page → token → gamerxyt interstitial → R2 presigned URL
 *   greenmotors.*  4KHDHub's gate: a nested atob/ROT13 blob hiding the host URL
 *
 * Two field notes that shaped this file:
 *  - Cloudflare R2 presigned URLs are signed for GET only: a HEAD returns 403.
 *    Nothing here uses HEAD.
 *  - R2 serves every object as application/octet-stream, so the *filename* is
 *    the only evidence of whether something is an MP4 or an MKV.
 */

const { fetchText, isSafeHttpUrl } = require('./util');

const MAX_HOPS = 4;

/* ------------------------------------------------------------------ helpers */

function rot13(s) {
  return String(s).replace(/[a-zA-Z]/g, (c) => {
    const base = c <= 'Z' ? 65 : 97;
    return String.fromCharCode(((c.charCodeAt(0) - base + 13) % 26) + base);
  });
}

function decodeEntities(s) {
  return String(s || '')
    .replace(/&amp;/g, '&')
    .replace(/&#0?38;/g, '&')
    .replace(/&quot;/g, '"')
    .replace(/&#0?39;/g, "'")
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>');
}

/** Pull anchor hrefs out of a fragment, label + url, in document order. */
function anchors(html, { scope = null } = {}) {
  const hay = scope ? html.slice(0, html.length) : html;
  const out = [];
  const re = /<a\b([^>]*)>([\s\S]*?)<\/a>/gi;
  let m;
  while ((m = re.exec(hay))) {
    const attrs = m[1] || '';
    const href = (attrs.match(/href\s*=\s*["']([^"']+)["']/i) || [])[1];
    if (!href) continue;
    const label = decodeEntities(m[2].replace(/<[^>]*>/g, ' '))
      .replace(/\s+/g, ' ')
      .trim();
    out.push({ href: decodeEntities(href), label, attrs });
  }
  return out;
}

function hostOf(url) {
  try {
    return new URL(url).hostname.toLowerCase();
  } catch {
    return '';
  }
}

/** Which host family a URL belongs to. */
function hostKind(url) {
  const h = hostOf(url);
  if (h.includes('pixeldrain')) return 'pixeldrain';
  if (h.includes('hubdrive')) return 'hubdrive';
  if (h.includes('hubcloud')) return 'hubcloud';
  if (h.includes('greenmotors') || h.includes('greenmountmotors')) return 'greenmotors';
  if (h.includes('gdflix') || h.includes('ddflix') || h.includes('gdlink')) return 'gdflix';
  if (h.endsWith('r2.dev')) return 'r2'; // public R2 bucket: ranged already
  if (h.includes('r2.cloudflarestorage.com')) return 'r2';
  if (h.includes('googleusercontent.com')) return 'direct'; // Google video CDN
  if (h.includes('fuckingfast')) return 'fuckingfast';
  return 'other';
}

/** Rank the buttons a hubcloud token page offers, best first. */
function rankCandidate(url) {
  const h = hostOf(url);
  if (h.includes('r2.cloudflarestorage.com')) return 0; // presigned, ranged
  if (h.includes('pixeldrain')) return 1; // ranged video
  if (h.includes('workers.dev')) return 6; // frequently 500
  if (h.includes('fuckingfast')) return 7; // yet another gate
  return 4;
}

/* ------------------------------------------------------------------- recipes */

/** pixeldrain: the URL is already the file. */
function resolvePixeldrain(url) {
  try {
    const u = new URL(url);
    // /u/<id>, /file/<id>, /api/file/<id>  →  /api/file/<id>?download
    const m = u.pathname.match(/\/(?:api\/file|u|file)\/([A-Za-z0-9_-]+)/);
    if (!m) return null;
    return `https://pixeldrain.com/api/file/${m[1]}?download`;
  } catch {
    return null;
  }
}

/** 4KHDHub's greenmotors gate: s('o','<BLOB>',…), an atob/ROT13 blob chain. */
function decodeGreenmotors(html) {
  const m = html.match(/s\(\s*'o'\s*,\s*'([^']+)'/);
  if (!m) return null;
  const b64 = (s) => Buffer.from(String(s), 'base64').toString('binary');

  // Field reports disagree on the exact nesting (two atob steps vs three), so
  // try each order and keep whatever actually parses to an { o } payload.
  const pipelines = [
    (b) => JSON.parse(b64(rot13(b64(b64(b))))),
    (b) => JSON.parse(b64(rot13(b64(b)))),
    (b) => JSON.parse(b64(b64(rot13(b64(b))))),
    (b) => JSON.parse(b64(b64(b))),
    (b) => JSON.parse(rot13(b64(b64(b)))),
  ];
  for (const run of pipelines) {
    try {
      const json = run(m[1]);
      if (json && json.o) {
        const target = b64(json.o);
        if (isSafeHttpUrl(target)) return target;
      }
    } catch {
      /* try the next shape */
    }
  }
  return null;
}

/** hubdrive.pics is only a selector: it links onward to hubcloud. */
function findHubcloudLink(html, base) {
  const links = anchors(html)
    .map((a) => a.href)
    .filter((h) => /hubcloud/i.test(h) && isSafeHttpUrl(h));
  if (links.length) return links[0];
  // fall back to a bare /drive/<id> reference anywhere on the page
  const m = html.match(/https?:\/\/[^"'\s]*hubcloud[^"'\s]*\/drive\/[A-Za-z0-9]+/i);
  if (m) return m[0];
  const rel = html.match(/\/drive\/([A-Za-z0-9]+)/);
  if (rel && base) return `https://hubcloud.ist/drive/${rel[1]}`;
  return null;
}

/** hubcloud.ist: page → token → interstitial → presigned R2 link. */
async function resolveHubcloud(url, ctx) {
  const page = await fetchText(url, { timeoutMs: ctx.timeoutMs });
  if (!page.ok) return { ok: false, error: `hubcloud page: ${page.error}` };

  let token =
    (page.text.match(/<a[^>]+id\s*=\s*["']download["'][^>]+href\s*=\s*["']([^"']+)["']/i) || [])[1] ||
    (page.text.match(/var\s+url\s*=\s*'([^']+)'/i) || [])[1] ||
    (page.text.match(/<a[^>]+href\s*=\s*["'](https?:\/\/[^"']*hubcloud\.php[^"']*)["']/i) || [])[1];
  if (!token) {
    // Some skins hide the token behind a second page load.
    const alt = anchors(page.text).find((a) => /download|server|fsl|buzz/i.test(a.label));
    token = alt && alt.href;
  }
  if (!token) return { ok: false, error: 'hubcloud: no token link found' };
  token = decodeEntities(token);
  if (token.startsWith('//')) token = `https:${token}`;
  if (!isSafeHttpUrl(token)) return { ok: false, error: 'hubcloud: unsafe token url' };

  const gate = await fetchText(token, { timeoutMs: ctx.timeoutMs });
  if (!gate.ok) return { ok: false, error: `hubcloud gate: ${gate.error}` };

  const candidates = anchors(gate.text)
    .map((a) => ({ ...a, href: a.href.startsWith('//') ? `https:${a.href}` : a.href }))
    .filter((a) => isSafeHttpUrl(a.href))
    // ignore obvious chrome/navigation
    .filter((a) => !/^(#|javascript:)/i.test(a.href))
    .filter((a) => !/hubcloud\.(php|ist)\/(drive|video|pack)?$/i.test(a.href));

  const preferred = candidates
    .filter((a) => rankCandidate(a.href) < 4)
    .sort((a, b) => rankCandidate(a.href) - rankCandidate(b.href));

  if (preferred.length) {
    return { ok: true, directUrl: preferred[0].href, via: 'hubcloud', label: preferred[0].label };
  }
  // Nothing ranged — hand back the best remaining button so the UI can at
  // least offer it as an external download rather than a dead end.
  const fallback = candidates.sort((a, b) => rankCandidate(a.href) - rankCandidate(b.href))[0];
  if (fallback) return { ok: true, directUrl: fallback.href, via: 'hubcloud', label: fallback.label, weak: true };
  return { ok: false, error: 'hubcloud: no server buttons on gate page' };
}

/* --------------------------------------------------------------------- entry */

/**
 * Resolve any supported file-host URL to a direct link.
 * Never throws. Follows at most MAX_HOPS gates.
 *
 * @returns {Promise<{ok:boolean, directUrl?:string, kind?:string, via?:string[], note?:string, error?:string}>}
 */
async function resolveHost(url, opts = {}) {
  const ctx = { timeoutMs: opts.timeoutMs || 12000, log: opts.log || (() => {}) };
  if (!isSafeHttpUrl(url)) return { ok: false, error: 'unsafe url' };

  const via = [];
  let current = url;

  for (let hop = 0; hop < MAX_HOPS; hop += 1) {
    const kind = hostKind(current);
    via.push(kind);

    if (kind === 'r2' || kind === 'pixeldrain' || kind === 'direct') {
      const direct = kind === 'pixeldrain' ? resolvePixeldrain(current) || current : current;
      return { ok: true, directUrl: direct, kind, via };
    }

    if (kind === 'hubdrive') {
      const page = await fetchText(current, { timeoutMs: ctx.timeoutMs });
      if (!page.ok) return { ok: false, via, error: `hubdrive: ${page.error}` };
      const next = findHubcloudLink(page.text, page.url);
      if (!next) return { ok: false, via, error: 'hubdrive: no hubcloud link on page' };
      current = next;
      continue;
    }

    if (kind === 'hubcloud') {
      const res = await resolveHubcloud(current, ctx);
      if (!res.ok) return { ok: false, via, error: res.error };
      if (res.weak) return { ok: true, directUrl: res.directUrl, kind: 'gate', via, note: 'external download host' };
      current = res.directUrl;
      continue;
    }

    if (kind === 'greenmotors') {
      const page = await fetchText(current, { timeoutMs: ctx.timeoutMs });
      if (!page.ok) return { ok: false, via, error: `greenmotors: ${page.error}` };
      const target = decodeGreenmotors(page.text);
      if (!target) return { ok: false, via, error: 'greenmotors: blob decode failed' };
      current = target;
      continue;
    }

    return { ok: false, via, error: `unsupported host: ${hostOf(current) || 'unknown'}` };
  }

  return { ok: false, via, error: 'too many redirect hops' };
}

module.exports = { resolveHost, hostKind, hostOf, resolvePixeldrain, decodeGreenmotors };
