'use strict';
/**
 * freaky-backup · unpack.js
 *
 * De-obfuscation helpers for the JS blobs the embed players hide their media
 * URLs in.
 *
 * The Streamwish / Filelions / StreamHG family serve their player config as a
 * Dean Edwards "p,a,c,k,e,d" packed `eval(...)`. That packer is a pure,
 * deterministic string substitution — the payload, a radix, a word count and a
 * keyword table — so it can be reversed with string work alone. Nothing here
 * executes fetched JavaScript, which matters: this runs on our server, and the
 * input is hostile by definition.
 *
 * The output is only ever scanned for media URLs; we never eval the result.
 */

/**
 * The packer's index encoder: base-`a` with a 36-symbol alphanumeric tail.
 * `encode(0)` is '' — which is why the dictionary lookup needs the raw key.
 */
function packerEncode(c, a) {
  const head = c < a ? '' : packerEncode(Math.floor(c / a), a);
  const rem = c % a;
  return head + (rem > 35 ? String.fromCharCode(rem + 29) : rem.toString(36));
}

/** Undo the escapes of a JS single/double-quoted string literal. */
function unescapeJsString(raw) {
  return raw.replace(/\\(x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|.)/g, (m, esc) => {
    if (esc[0] === 'x') return String.fromCharCode(parseInt(esc.slice(1), 16));
    if (esc[0] === 'u') return String.fromCharCode(parseInt(esc.slice(1), 16));
    if (esc === 'n') return '\n';
    if (esc === 'r') return '\r';
    if (esc === 't') return '\t';
    if (esc === 'b') return '\b';
    if (esc === 'f') return '\f';
    return esc; // \' \" \\ \/ and anything else is itself
  });
}

/** Read a quoted JS string starting at `start` (index of the quote). */
function readJsString(src, start) {
  const quote = src[start];
  let out = '';
  let i = start + 1;
  while (i < src.length) {
    const ch = src[i];
    if (ch === '\\') {
      out += ch + (src[i + 1] ?? '');
      i += 2;
      continue;
    }
    if (ch === quote) return { value: unescapeJsString(out), end: i + 1 };
    out += ch;
    i += 1;
  }
  return null;
}

/**
 * Unpack every `eval(function(p,a,c,k,e,d){…}('payload',radix,count,'words'.split('|'),0,{}))`
 * block found in `source`, and return the concatenated unpacked text.
 *
 * Returns '' when there is nothing packed — callers should fall back to
 * scanning the original text.
 */
function unpackPacker(source) {
  const text = String(source || '');
  const results = [];
  // Find each eval( ... ) that contains the packer signature.
  const startRe = /eval\s*\(\s*function\s*\(\s*p\s*,\s*a\s*,\s*c\s*,\s*k\s*,\s*e\s*,\s*d\s*\)/g;
  let m;
  while ((m = startRe.exec(text))) {
    const bodyStart = m.index + m[0].length;
    // Walk the outer call to find the argument list.
    let depth = 1;
    let i = bodyStart;
    while (i < text.length && depth > 0) {
      const ch = text[i];
      if (ch === '(') depth += 1;
      else if (ch === ')') depth -= 1;
      i += 1;
    }
    const call = text.slice(bodyStart, i - 1);

    // The argument list follows the function body's closing brace, so find
    // that brace by counting from the body's opening one. Neither
    // lastIndexOf('(') nor lastIndexOf('}') works here: the payload may
    // contain the former, and the call always ends with a `{}` argument.
    const braceStart = call.indexOf('{');
    if (braceStart === -1) continue;
    let depth2 = 0;
    let j = braceStart;
    for (; j < call.length; j += 1) {
      if (call[j] === '{') depth2 += 1;
      else if (call[j] === '}') {
        depth2 -= 1;
        if (depth2 === 0) break;
      }
    }
    if (depth2 !== 0) continue;
    const argList = call.slice(j + 1).replace(/^\s*\(/, '');
    const payload = readJsString(argList, 0);
    if (!payload) continue;

    const rest = argList.slice(payload.end);
    const nums = rest.match(/,\s*(\d+)\s*,\s*(\d+)\s*,/);
    if (!nums) continue;
    const radix = parseInt(nums[1], 10);
    const count = parseInt(nums[2], 10);

    // The keyword table is a quoted string followed by .split('|')
    const wordsAt = rest.indexOf(nums[0]) + nums[0].length;
    const wordsLit = readJsString(rest, wordsAt);
    if (!wordsLit) continue;
    const words = wordsLit.value.split('|');

    // Build the dictionary exactly as the packer's runtime does.
    const dict = {};
    for (let idx = count - 1; idx >= 0; idx -= 1) {
      const key = packerEncode(idx, radix);
      dict[key] = words[idx] !== undefined && words[idx] !== '' ? words[idx] : key;
    }

    // Substitute \b<key>\b everywhere in the payload.
    const unpacked = payload.value.replace(/\b[0-9A-Za-z_$]+\b/g, (tok) =>
      Object.prototype.hasOwnProperty.call(dict, tok) ? dict[tok] : tok
    );
    results.push(unpacked);
  }
  return results.join('\n');
}

/** Media URLs worth looking for in a player config or an unpacked blob. */
const MEDIA_URL_RE =
  /(?:https?:)?\/\/[^\s"'<>\\)]+?\.(?:m3u8|mp4|m4v|webm|mpd)(?:\?[^\s"'<>\\)]*)?/gi;

/** JWPlayer-ish `file:` / `source:` / `src:` assignments. */
const CONFIG_URL_RE =
  /(?:file|source|src|url|hls\d?|video_url|playlist)\s*[:=]\s*["']([^"']+)["']/gi;

function normaliseMediaUrl(u) {
  let out = String(u || '').trim();
  if (out.startsWith('//')) out = `https:${out}`;
  return out;
}

/**
 * Pull candidate media URLs out of arbitrary text — raw URLs anywhere, plus
 * `file:`/`source:`-style config assignments. Deduped, order preserved.
 */
function findMediaUrls(text) {
  const src = String(text || '');
  const out = [];
  const seen = new Set();
  const push = (u) => {
    const url = normaliseMediaUrl(u).replace(/\\\//g, '/');
    if (!url || seen.has(url)) return;
    if (!/^https?:\/\//i.test(url)) return;
    seen.add(url);
    out.push(url);
  };

  let m;
  MEDIA_URL_RE.lastIndex = 0;
  while ((m = MEDIA_URL_RE.exec(src))) push(m[0]);

  CONFIG_URL_RE.lastIndex = 0;
  while ((m = CONFIG_URL_RE.exec(src))) {
    const v = m[1].replace(/\\\//g, '/');
    if (/\.(m3u8|mp4|m4v|webm|mpd)(\?|$)/i.test(v) || /\/hls\//i.test(v)) push(v);
  }
  return out;
}

/**
 * Full pass: scan raw text, and if that yields nothing useful, unpack the
 * packer blobs and scan again. Returns { urls, usedUnpack }.
 */
function extractMediaUrls(source) {
  const direct = findMediaUrls(source);
  const unpacked = unpackPacker(source);
  if (!unpacked) return { urls: direct, usedUnpack: false, unpacked: '' };
  const fromPacked = findMediaUrls(unpacked);
  const merged = [...direct];
  for (const u of fromPacked) if (!merged.includes(u)) merged.push(u);
  return { urls: merged, usedUnpack: true, unpacked };
}

module.exports = {
  unpackPacker,
  extractMediaUrls,
  findMediaUrls,
  packerEncode,
  unescapeJsString,
};
