'use strict';
/**
 * freaky-backup · sites.js
 *
 * Direct Tamil-site extractors — the non-addon half of the backup system.
 *
 * Stremio addons give us torrents and a few file-host links. The Tamil sites
 * themselves carry download buttons pointing at the same file hosts, often for
 * releases the addons miss, and (via resolvers.js) they can become plain HTTP
 * files that need no P2P at all.
 *
 * These sites rotate domains every few weeks and differ only in markup
 * details, so a site is **configuration, not code**: give it candidate domains,
 * a search path, and the file hosts its download buttons use. Adding or
 * repairing a site is a config edit.
 *
 *   {
 *     "name": "TamilMV", "tag": "TMV", "driver": "site",
 *     "domains": ["1tamilmv.tel", "1tamilmv.xyz"],
 *     "searchPath": "/index.php?/search/&q={q}",
 *     "linkHosts": ["hubcloud", "hubdrive", "gdflix", "pixeldrain"]
 *   }
 *
 * Everything here is best-effort: a site that is down, challenged or rearranged
 * returns zero streams and never breaks the request.
 */

const { fetchText, isSafeHttpUrl } = require('./util');

const DOMAIN_TTL_MS = 30 * 60 * 1000;
/** domain cache: driverKey -> { base, checkedAt, ok } */
const domainCache = new Map();

/* ------------------------------------------------------------------ matching */

function norm(s) {
  return String(s || '')
    .toLowerCase()
    .replace(/[^a-z0-9\s]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function tokens(s) {
  return norm(s)
    .split(' ')
    .filter((t) => t.length > 2);
}

/**
 * Does a candidate page title look like the film we asked for?
 * Deliberately lenient: a false negative loses a source, which is worse than
 * surfacing an extra row the user can ignore.
 */
function titleMatches(candidate, want, year) {
  const c = norm(candidate);
  const w = norm(want);
  if (!c || !w) return false;
  if (c.includes(w) || w.includes(c)) {
    if (!year || c.includes(String(year)) || !/\b(19|20)\d{2}\b/.test(c)) return true;
  }
  const wt = tokens(want);
  if (!wt.length) return false;
  const hits = wt.filter((t) => c.includes(t)).length;
  const ratio = hits / wt.length;
  const yearOk = !year || c.includes(String(year));
  return ratio >= 0.6 && yearOk;
}

/** Turn a URL path into something title-like: /movie/sardar-2-2026/ -> "sardar 2 2026". */
function slugText(url) {
  try {
    const parts = new URL(url).pathname.split('/').filter(Boolean);
    const last = parts[parts.length - 1] || '';
    return decodeURIComponent(last).replace(/[-_+]+/g, ' ').replace(/\.(html?|php)$/i, '').trim();
  } catch {
    return '';
  }
}

function yearOf(text) {
  const m = String(text || '').match(/\b(19|20)\d{2}\b/);
  return m ? m[0] : '';
}

/* ------------------------------------------------------------------- domains */

function driverKey(site) {
  return site.name || site.tag || (site.domains || []).join(',');
}

/**
 * Is this real page markup, or a gate pretending to be one?
 *
 * Learned from probing the live Tamil sites: the ones that answer at all tend
 * to answer with a ~400-byte JS shell — an anti-bot bounce carrying a signed
 * token, a consent wall, or a Cloudflare interstitial. Treating those as "the
 * site is up" wastes a fan-out slot and yields nothing, so they are rejected
 * here and the driver moves to the next candidate domain.
 */
function isRealPage(text) {
  if (!text || text.length < 200) return false;
  if (/Just a moment|Attention Required|Checking your browser|cf-browser-verification|_cf_chl_/i.test(text)) return false;
  if (/<title>\s*Loading\.?\.?\.?\s*<\/title>/i.test(text)) return false;
  if (/window\.location\.replace\(/i.test(text) && text.length < 2500) return false;
  if (/gdprAppliesGlobally|cmp_id|cmp_cdid/i.test(text) && text.length < 3000) return false;
  return true;
}

/** Find a live base URL for a site, remembering which domain answered. */
async function liveBase(site, timeoutMs) {
  const key = driverKey(site);
  const hit = domainCache.get(key);
  if (hit && Date.now() - hit.checkedAt < DOMAIN_TTL_MS) return hit.ok ? hit.base : null;

  const domains = (site.domains || []).filter(Boolean);
  for (const raw of domains) {
    const base = /^https?:\/\//i.test(raw) ? raw.replace(/\/+$/, '') : `https://${raw.replace(/\/+$/, '')}`;
    if (!isSafeHttpUrl(base)) continue;
    const res = await fetchText(base, { timeoutMs: Math.min(timeoutMs, 9000) });
    if (res.ok && isRealPage(res.text)) {
      domainCache.set(key, { base: res.url ? new URL(res.url).origin : base, checkedAt: Date.now(), ok: true });
      return domainCache.get(key).base;
    }
  }
  domainCache.set(key, { base: null, checkedAt: Date.now(), ok: false });
  return null;
}

/* -------------------------------------------------------------- link hunting */

/** Pull every link on a page that points at one of the site's file hosts. */
function findHostLinks(html, base, linkHosts) {
  const hosts = (linkHosts || []).map((h) => String(h).toLowerCase());
  const found = [];
  const seen = new Set();

  const re = /<a\b[^>]*href\s*=\s*["']([^"']+)["'][^>]*>([\s\S]*?)<\/a>/gi;
  let m;
  while ((m = re.exec(html))) {
    let href = m[1].replace(/&amp;/g, '&').trim();
    if (!href || /^(#|javascript:|mailto:)/i.test(href)) continue;
    if (href.startsWith('//')) href = `https:${href}`;
    else if (href.startsWith('/')) {
      try {
        href = new URL(href, base).toString();
      } catch {
        continue;
      }
    }
    if (!isSafeHttpUrl(href)) continue;
    if (!hosts.some((h) => href.toLowerCase().includes(h))) continue;
    if (seen.has(href)) continue;
    seen.add(href);

    const label = m[2]
      .replace(/<[^>]*>/g, ' ')
      .replace(/&amp;/g, '&')
      .replace(/\s+/g, ' ')
      .trim();
    // The quality/size usually lives in the surrounding text, not the anchor.
    const around = html.slice(Math.max(0, m.index - 260), m.index + 260);
    found.push({ url: href, label, context: around });
  }
  return found;
}

/**
 * Pull `magnet:` links out of a page.
 *
 * These are the torrent half of a site's offering: an infoHash plus display
 * name and trackers. They cannot ride the normal URL path (the http(s) guard
 * rejects the scheme), so they are returned separately and surfaced as
 * Stremio-shaped `infoHash` entries — which the aggregator already knows how to
 * hand to the torrent engine and to the frontend's magnet list.
 */
function findMagnets(html) {
  const out = [];
  const seen = new Set();
  const re = /<a\b[^>]*href\s*=\s*["'](magnet:\?[^"']+)["'][^>]*>([\s\S]*?)<\/a>/gi;
  let m;
  while ((m = re.exec(html))) {
    const url = m[1].replace(/&amp;/g, '&');
    const ih = (url.match(/xt=urn:btih:([A-Za-z0-9]{32,40})/i) || [])[1];
    if (!ih) continue;
    const key = ih.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    let dn = '';
    try {
      dn = decodeURIComponent((url.match(/[?&]dn=([^&]+)/) || [])[1] || '').replace(/\+/g, ' ');
    } catch {
      dn = '';
    }
    const text = m[2].replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
    const around = html.slice(Math.max(0, m.index - 240), m.index + 240);
    out.push({ infoHash: key, magnet: url, label: dn || text || 'torrent', context: around });
  }
  return out;
}

/** Pick the page links that are film pages rather than navigation. */
function findPostLinks(html, base, want, year) {
  const out = [];
  const seen = new Set();
  const re = /<a\b[^>]*href\s*=\s*["']([^"']+)["'][^>]*>([\s\S]*?)<\/a>/gi;
  let m;
  while ((m = re.exec(html))) {
    let href = m[1].replace(/&amp;/g, '&').trim();
    const text = m[2]
      .replace(/<[^>]*>/g, ' ')
      .replace(/&amp;/g, '&')
      .replace(/&#\d+;/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();
    if (href.startsWith('//')) href = `https:${href}`;
    else if (href.startsWith('/') || !/^https?:/i.test(href)) {
      // Search results are almost always site-relative ("/leo-2023-tamil-movie/"),
      // so resolve against the search page before the safety checks.
      try {
        href = new URL(href, base).toString();
      } catch {
        continue;
      }
    }
    if (!isSafeHttpUrl(href)) continue;
    if (new URL(href).origin !== new URL(base).origin) continue;
    // Match on the anchor text OR the slug, not one in place of the other.
    // Listing rows are often bare posters, but just as often they all carry
    // the same generic label ("Download Now"), in which case only the slug
    // (/movie/charukesi-2026/) identifies the film.
    const slug = slugText(href);
    const byText = text && text.length >= 3 && titleMatches(text, want, year);
    const bySlug = slug && titleMatches(slug, want, year);
    if (!byText && !bySlug) continue;
    if (seen.has(href)) continue;
    seen.add(href);
    out.push({ url: href, text: byText ? text : slug });
    if (out.length >= 6) break;
  }
  return out;
}

/* -------------------------------------------------------------------- driver */

/**
 * Run one site driver.
 *
 * @param {object} site  the upstream config block
 * @param {{title:string, year?:string, type?:string, season?:number, episode?:number}} ctx
 * @returns {Promise<{ok:boolean, streams:Array, ms:number, error?:string, searched?:string}>}
 */

/**
 * Same-origin links that look like a step towards the file rather than chrome.
 * Moviesda's chain is search -> /movie/<slug>/ -> /original/ -> /1080p-hd/ ->
 * /download/<slug>-<quality>/, and every one of those steps is discoverable
 * from the previous page's own anchors.
 */
function interestingLinks(html, pageUrl, origin) {
  const out = [];
  const seen = new Set();
  const re = /<a\b[^>]*href\s*=\s*["']([^"']+)["'][^>]*>([\s\S]*?)<\/a>/gi;
  let m;
  while ((m = re.exec(html))) {
    let href = m[1].replace(/&amp;/g, '&').trim();
    const label = m[2].replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
    if (!href || /^(#|javascript:|mailto:)/i.test(href)) continue;
    try {
      href = new URL(href, pageUrl).toString();
    } catch {
      continue;
    }
    if (!isSafeHttpUrl(href)) continue;
    if (origin && new URL(href).origin !== origin) continue;
    const blob = `${href} ${label}`;
    if (!/original|download|\b(2160p|1080p|720p|480p|4k|uhd|hd)\b|quality/i.test(blob)) continue;
    if (seen.has(href)) continue;
    seen.add(href);
    out.push(href);
    if (out.length >= 8) break;
  }
  return out;
}

/**
 * Walk a matched post's own links looking for file-host links.
 *
 * Moviesda-style sites never put the download button on the page you land on,
 * so a single fetch misses them entirely. Bounded by depth and page count, and
 * it stops at the first depth that yields something.
 */
async function crawlForLinks(startUrl, base, opts) {
  const { linkHosts, timeoutMs, want, year } = opts;
  const maxDepth = Math.min(opts.maxDepth ?? 3, 4);
  const maxPages = opts.maxPages ?? 3;
  const origin = new URL(base).origin;
  const seen = new Set([startUrl]);
  let frontier = [startUrl];
  const found = [];

  for (let depth = 0; depth <= maxDepth && frontier.length; depth += 1) {
    const next = [];
    for (const pageUrl of frontier.slice(0, maxPages)) {
      const page = await fetchText(pageUrl, { timeoutMs });
      if (!page.ok) continue;
      const here = page.url || pageUrl;
      for (const link of findHostLinks(page.text, here, linkHosts)) {
        if (!found.some((f) => f.url === link.url)) found.push({ ...link, depth });
      }
      for (const mag of findMagnets(page.text)) {
        if (!found.some((f) => f.infoHash === mag.infoHash)) found.push({ ...mag, depth });
      }
      if (found.length) return found; // first depth that pays out wins
      if (depth === maxDepth) continue;
      for (const link of interestingLinks(page.text, here, origin)) {
        if (!seen.has(link)) {
          seen.add(link);
          next.push(link);
        }
      }
    }
    frontier = next;
  }
  return found;
}

async function runSite(site, ctx, timeoutMs = 15000) {
  const started = Date.now();
  const want = ctx.title;
  if (!want) return { ok: false, streams: [], ms: 0, error: 'site driver needs a title' };

  const base = await liveBase(site, timeoutMs);
  if (!base) return { ok: false, streams: [], ms: Date.now() - started, error: 'no live domain' };

  const q = encodeURIComponent(`${want}${ctx.year ? ` ${ctx.year}` : ''}`);
  const path = String(site.searchPath || '/?s={q}').replace('{q}', q);
  const searchUrl = path.startsWith('http') ? path : `${base}${path.startsWith('/') ? '' : '/'}${path}`;

  const search = await fetchText(searchUrl, { timeoutMs });
  if (!search.ok) {
    return { ok: false, streams: [], ms: Date.now() - started, error: `search: ${search.error}`, searched: searchUrl };
  }

  let posts = findPostLinks(search.text, search.url || base, want, ctx.year || yearOf(want));
  let searched = searchUrl;

  // Some of these sites have a search form that is simply not wired up
  // server-side (Moviesda ignores ?q= entirely and always returns the default
  // listing). Fall back to walking their listing pages and matching slugs.
  if (!posts.length && Array.isArray(site.listingPaths) && site.listingPaths.length) {
    const pages = Math.min(site.listingPages || 3, 6);
    outer: for (const tmpl of site.listingPaths) {
      for (let page = 1; page <= pages; page += 1) {
        const listUrl = tmpl.startsWith('http')
          ? tmpl.replace('{page}', page)
          : `${base}${tmpl.startsWith('/') ? '' : '/'}${tmpl}`.replace('{page}', page);
        const listing = await fetchText(listUrl, { timeoutMs });
        if (!listing.ok) continue;
        const found = findPostLinks(listing.text, listing.url || base, want, ctx.year || yearOf(want));
        if (found.length) {
          posts = found;
          searched = listUrl;
          break outer;
        }
      }
    }
  }

  if (!posts.length) {
    return { ok: true, streams: [], ms: Date.now() - started, searched, error: 'no matching results' };
  }

  const streams = [];
  const seenUrl = new Set();
  for (const post of posts.slice(0, (site.maxPages || 3))) {
    const links = await crawlForLinks(post.url, search.url || base, {
      linkHosts: site.linkHosts,
      timeoutMs,
      want,
      year: ctx.year || yearOf(want),
      maxDepth: site.crawlDepth,
      maxPages: site.maxPagesPerDepth,
    });
    for (const link of links) {
      const dedupe = link.url || `magnet:${link.infoHash}`;
      if (seenUrl.has(dedupe)) continue;
      seenUrl.add(dedupe);

      if (link.infoHash && !link.url) {
        const q = (link.context.match(/\b(2160p|4k|uhd|1080p|720p|480p)\b/i) || [])[1] || '';
        streams.push({
          name: `${site.tag || site.name} ${q || 'torrent'}`.trim(),
          title: [link.label, q].filter(Boolean).join('\n'),
          infoHash: link.infoHash,
          sources: (link.magnet.match(/[?&]tr=([^&]+)/g) || []).map((t) =>
            decodeURIComponent(t.replace(/^[?&]tr=/, ''))
          ),
          _site: site.name,
        });
        continue;
      }
      const quality =
        (link.context.match(/\b(2160p|4k|uhd|1080p|720p|480p)\b/i) || [])[1] ||
        (post.text.match(/\b(2160p|4k|uhd|1080p|720p|480p)\b/i) || [])[1] ||
        '';
      const size = (link.context.match(/\b\d+(?:\.\d+)?\s*(?:GB|MB)\b/i) || [])[0] || '';
      streams.push({
        name: `${site.tag || site.name} ${quality || 'file'}`.trim(),
        title: [post.text, quality, size, link.label].filter(Boolean).join('\n'),
        url: link.url,
        _site: site.name,
      });
    }
    if (streams.length >= 40) break;
  }

  return {
    ok: true,
    streams,
    ms: Date.now() - started,
    searched: searchUrl,
    error: streams.length ? undefined : 'no file-host links on matched pages',
  };
}

/** A site upstream is any config block with driver:"site". */
function isSiteUpstream(upstream) {
  return Boolean(upstream && (upstream.driver === 'site' || Array.isArray(upstream.domains)));
}

/** Probe a site for /stats + startup logging. */
async function probeSite(site, timeoutMs = 10000) {
  const started = Date.now();
  const base = await liveBase(site, timeoutMs);
  return { ok: Boolean(base), base, ms: Date.now() - started, error: base ? undefined : 'no live domain' };
}

module.exports = { runSite, isSiteUpstream, probeSite, findHostLinks, findMagnets, findPostLinks, titleMatches, crawlForLinks, interestingLinks, slugText };
