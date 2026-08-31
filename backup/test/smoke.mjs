#!/usr/bin/env node
'use strict';
/**
 * freaky-backup · smoke test
 * End-to-end checks against running instances:
 *   node test/smoke.mjs [englishBase] [tamilBase]
 */

const EN = process.argv[2] || 'http://127.0.0.1:8101';
const TA = process.argv[3] || 'http://127.0.0.1:8102';

let pass = 0;
let fail = 0;
function check(name, cond, detail = '') {
  if (cond) { pass++; console.log(`  ✓ ${name}${detail ? ` — ${detail}` : ''}`); }
  else { fail++; console.log(`  ✗ FAIL: ${name}${detail ? ` — ${detail}` : ''}`); }
}

async function getJson(url, timeoutMs = 60000) {
  const controller = new AbortController();
  const t = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, { signal: controller.signal });
    const text = await res.text();
    let data = null;
    try { data = JSON.parse(text); } catch { /* keep null */ }
    return { status: res.status, ok: res.ok, data, text };
  } finally {
    clearTimeout(t);
  }
}

async function main() {
  console.log(`\n== smoke test against\n   EN: ${EN}\n   TA: ${TA}\n`);

  // ---- manifests -----------------------------------------------------------
  console.log('[manifests]');
  {
    const en = await getJson(`${EN}/manifest.json`);
    check('EN manifest 200', en.status === 200);
    check('EN manifest shape', en.data && en.data.id && Array.isArray(en.data.resources));
    const ta = await getJson(`${TA}/manifest.json`);
    check('TA manifest 200', ta.status === 200);
    check('TA distinct addon ids', en.data && ta.data && en.data.id !== ta.data.id);

    for (const [label, base] of [['EN', EN], ['TA', TA]]) {
      const html = await fetch(base).then((r) => r.text());
      check(`${label} landing page`, html.includes('<!doctype html>') && html.toLowerCase().includes('install in stremio'));
      const hz = await getJson(`${base}/healthz`);
      check(`${label} healthz`, hz.status === 200 && hz.data.ok === true);
    }
  }

  // ---- english movie -------------------------------------------------------
  console.log('\n[english movie — Dune: Part Two tt15239678]');
  {
    const r = await getJson(`${EN}/stream/movie/tt15239678.json`);
    check('HTTP 200 + streams[]', r.status === 200 && Array.isArray(r.data && r.data.streams), `${(r.data && r.data.streams || []).length} streams, status ${r.status}`);
    if (!Array.isArray(r.data && r.data.streams)) { console.log('   body:', (r.text || '').slice(0, 300)); }
    const direct = (r.data.streams || []).filter((s) => s.url);
    check('has direct-URL streams', direct.length > 0, `${direct.length} direct`);
    check('HDHub source tagged', (r.data.streams||[]).some((s) => /^(\[HDHub\])|\[HDHub\]/.test(s.name) || /⚡direct/.test(s.name)));
    const upstreams = direct.filter((s) => /^https?:\/\//.test(s.url || '') && !s.url.includes('/d/')).slice(0, 3);
    if (upstreams.length) {
      // probe up to 3 upstream links; pass if ANY serves media bytes
      let served = null;
      const attempts = [];
      for (const cand of upstreams) {
        try {
          const probe = await fetch(cand.url, { method: 'GET', headers: { Range: 'bytes=0-1023' }, signal: AbortSignal.timeout(15000), redirect: 'follow' });
          await probe.arrayBuffer();
          attempts.push(`${new URL(cand.url).host}:${probe.status}`);
          if ([200, 206].includes(probe.status)) { served = { host: new URL(cand.url).host, status: probe.status }; break; }
        } catch (e) {
          attempts.push(`${new URL(cand.url).host}:ERR`);
        }
      }
      check('upstream direct URL serves bytes', Boolean(served),
        served ? `${served.status} from ${served.host}` : `all failed [${attempts.join(', ')}]`);
    }
    // engine end-to-end: cold torrent attach can take up to ~45s for metadata
    const eng = direct.find((s) => s.url && s.url.includes('/d/'));
    if (eng) {
      try {
        const t0 = Date.now();
        const probe = await fetch(eng.url, { method: 'GET', headers: { Range: 'bytes=0-262143' }, signal: AbortSignal.timeout(90000) });
        const buf = await probe.arrayBuffer();
        const secs = Math.round((Date.now() - t0) / 1000);
        check('torrent→direct engine serves bytes', [200, 206].includes(probe.status) && buf.byteLength > 100000,
          `HTTP ${probe.status}, ${buf.byteLength} bytes in ${secs}s`);
      } catch (e) {
        check('torrent→direct engine serves bytes', false, e.message);
      }
    }
  }

  // ---- english series ------------------------------------------------------
  console.log('\n[english series — Breaking Bad S01E01 tt0903747:1:1]');
  {
    const r = await getJson(`${EN}/stream/series/tt0903747:1:1.json`);
    check('HTTP 200 + streams[]', r.status === 200 && Array.isArray(r.data.streams), `${(r.data.streams || []).length} streams`);
    check('torrentio results present', (r.data.streams||[]).some((s) => s.name.includes('[Torrentio]') || s.name.includes('[TTorrent]')));
  }

  // ---- tamil movie ---------------------------------------------------------
  console.log('\n[tamil movie — Vikram tt9179430]');
  {
    const r = await getJson(`${TA}/stream/movie/tt9179430.json?debug=1`);
    check('HTTP 200 + streams[]', r.status === 200 && Array.isArray(r.data.streams), `${(r.data.streams || []).length} streams`);
    check('tamil tracker torrents present',
      (r.data.streams||[]).some((s) => s.infoHash || s.freaky?.kind === 'torrent' || /tamil/i.test(s.title || '')),
      'infoHash or tamil-title stream found');
    check('source diagnostics included', Array.isArray(r.data.sources) && r.data.sources.length >= 2);
    // dual-audio hindi+tamil result seen during research; don't hard-fail if trackers shift
    const tamilish = (r.data.streams||[]).find((s) => /tamil/i.test(`${s.title} ${s.name}`));
    if (tamilish) console.log(`    sample: ${String(tamilish.title).slice(0, 80)}`);
  }

  // ---- desiflix upstream on both --------------------------------------------
  console.log('\n[DesiFlix upstream reachability via aggregation]');
  {
    const r = await getJson(`${EN}/api/streams?type=movie&id=tt0111161`); // Shawshank
    check('EN Shawshank has streams', r.data.total > 0, `${r.data.total} total`);
    const ta = await getJson(`${TA}/api/streams?type=movie&id=tt9179430&debug=1`);
    check('TA sources array lists all upstreams', ta.data.sources.every((s) => 'ok' in s));
  }

  // ---- validation / hardening ------------------------------------------------
  console.log('\n[hardening]');
  {
    const bad = await getJson(`${EN}/stream/movie/%2e%2e%2fetc%2fpasswd.json`);
    check('path traversal rejected', bad.status === 400);
    const bad2 = await getJson(`${EN}/stream/movie/notanid.json`);
    check('garbage id rejected', bad2.status === 400);
    const eng404 = await getJson(`${EN}/nope.json`);
    check('unknown route → 404 json', eng404.status === 404 && eng404.data.error === 'not found');
  }

  console.log(`\n== RESULT: ${pass} passed, ${fail} failed\n`);
  process.exit(fail ? 1 : 0);
}

main().catch((e) => { console.error('smoke crashed:', e); process.exit(1); });
