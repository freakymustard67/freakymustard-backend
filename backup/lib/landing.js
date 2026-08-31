'use strict';
/**
 * freaky-backup · landing.js
 * Self-contained HTML landing page per instance.
 */

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function landingPage(cfg, origin, upstreamStatus) {
  const rows = cfg.upstreams.map((u) => {
    const st = upstreamStatus.get(u.name);
    const state = !st ? '…' : st.ok ? `✓ ${st.count ?? 0} streams · ${st.ms}ms` : `✗ ${esc(st.error || 'error')}`;
    return `<tr><td>${esc(u.name)}</td><td class="mono">${state}</td></tr>`;
  }).join('\n');

  const engine = cfg.engine && cfg.engine.enabled;
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>${esc(cfg.displayName)} — Stremio backup stream server</title>
<style>
  :root { --accent:${cfg.accent}; --accent2:${cfg.accent2}; }
  * { box-sizing:border-box; margin:0; }
  body { font-family:ui-sans-serif,system-ui,'Segoe UI',Roboto,sans-serif; background:#0b0e14; color:#e6e9f0; min-height:100vh;
         display:flex; align-items:center; justify-content:center; padding:32px 16px; }
  .card { max-width:720px; width:100%; background:linear-gradient(160deg,#11151f,#0d1018); border:1px solid #232a3a;
          border-radius:18px; padding:36px; box-shadow:0 20px 60px rgba(0,0,0,.5); }
  h1 { font-size:26px; letter-spacing:-.02em; }
  h1 .dot { color:var(--accent); }
  .sub { color:#8b93a7; margin:10px 0 22px; line-height:1.55; font-size:14px; }
  .btn { display:inline-block; background:linear-gradient(90deg,var(--accent),var(--accent2)); color:#08101c; font-weight:700;
         padding:12px 22px; border-radius:10px; text-decoration:none; font-size:15px; }
  .btn:hover { filter:brightness(1.1); }
  code.mono, .mono { font-family:ui-monospace,'Cascadia Code',Menlo,monospace; font-size:12.5px; color:#9fb4d8; word-break:break-all; }
  h2 { font-size:13px; text-transform:uppercase; letter-spacing:.14em; color:#66708a; margin:26px 0 10px; }
  table { width:100%; border-collapse:collapse; font-size:14px; }
  td { padding:7px 4px; border-bottom:1px solid #1b2130; vertical-align:top; }
  tr:last-child td { border-bottom:none; }
  ul { padding-left:18px; color:#aab3c5; font-size:13.5px; line-height:1.9; }
  a { color:var(--accent2); }
  footer { margin-top:26px; color:#525a70; font-size:12px; line-height:1.7; }
</style>
</head>
<body>
<div class="card">
  <h1>${esc(cfg.displayName)} <span class="dot">●</span> online</h1>
  <p class="sub">${esc(cfg.description)}</p>

  <a class="btn" href="stremio://${esc(origin)}/manifest.json">Install in Stremio</a>
  &nbsp; <a class="btn" style="background:#1a2130;color:#9fb4d8" href="/manifest.json">manifest.json</a>

  <h2>Upstream addons</h2>
  <table>${rows}</table>

  <h2>API</h2>
  <ul>
    <li><code class="mono">GET /stream/movie/tt15239678.json</code> — movie streams (Stremio format)</li>
    <li><code class="mono">GET /stream/series/tt0903747:1:1.json</code> — episode streams</li>
    <li><code class="mono">GET /api/streams?type=movie&amp;id=tt15239678</code> — same data + source diagnostics</li>
    <li><code class="mono">GET /healthz</code> · <code class="mono">GET /stats</code></li>
${engine ? '    <li>Torrent→direct-stream engine: torrent results additionally expose an HTTP url like <code class="mono">/d/&lt;infoHash&gt;/&lt;fileIdx&gt;/file.mp4</code> with Range support.</li>\n' : ''}  </ul>

  <footer>
    freaky-backup v${esc(cfg.version)} · instance “${esc(cfg.instance)}” · standalone backup for freaky mustard.<br/>
    Streams are resolved from public Stremio addons and played directly by your client.
  </footer>
</div>
</body>
</html>`;
}

module.exports = { landingPage };
