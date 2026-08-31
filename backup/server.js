#!/usr/bin/env node
'use strict';
/**
 * freaky-backup · server.js
 * Entry point: node server.js --config config/english.json
 */

const http = require('http');
const fs = require('fs');
const path = require('path');

const { createApp } = require('./lib/app');
const { log } = require('./lib/util');

function parseArgs(argv) {
  const out = {};
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--config') out.config = argv[++i];
    else if (a === '--port') out.port = parseInt(argv[++i], 10);
    else if (a === '--host') out.host = argv[++i];
  }
  return out;
}

async function main() {
  const args = parseArgs(process.argv);
  if (!args.config) {
    console.error('usage: node server.js --config <config.json> [--port N] [--host 0.0.0.0]');
    process.exit(1);
  }
  const cfgPath = path.resolve(args.config);
  const cfg = JSON.parse(fs.readFileSync(cfgPath, 'utf8'));
  if (args.port) cfg.port = args.port;
  if (args.host) cfg.host = args.host;
  cfg.version = cfg.version || '1.0.0';

  const app = createApp(cfg);
  const server = http.createServer(app.handler);
  server.keepAliveTimeout = 65000;

  // warm upstream probes in background (non-blocking)
  app.probeUpstreams().catch(() => {});

  server.listen(cfg.port, cfg.host || '0.0.0.0', () => {
    log(cfg.instance, `${cfg.displayName} listening on http://${cfg.host || '0.0.0.0'}:${cfg.port}`);
    log(cfg.instance, `manifest:   http://127.0.0.1:${cfg.port}/manifest.json`);
    log(cfg.instance, `streams:    http://127.0.0.1:${cfg.port}/stream/movie/tt15239678.json`);
    log(cfg.instance, `engine:     ${cfg.engine && cfg.engine.enabled ? 'ENABLED (torrent→direct HTTP)' : 'disabled'}`);
  });

  let stopping = false;
  const stop = async (sig) => {
    if (stopping) return;
    stopping = true;
    log(cfg.instance, `received ${sig}, shutting down…`);
    await app.shutdown().catch(() => {});
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 3000).unref();
  };
  process.on('SIGINT', () => stop('SIGINT'));
  process.on('SIGTERM', () => stop('SIGTERM'));
}

main().catch((err) => {
  console.error('fatal:', err.stack || err.message);
  process.exit(1);
});

// Last-resort guards: a bad upstream payload or P2P edge case must NEVER take
// the whole backup server down. Log and keep serving.
process.on('uncaughtException', (err) => {
  console.error('[uncaughtException]', err.stack || err.message);
});
process.on('unhandledRejection', (err) => {
  console.error('[unhandledRejection]', (err && err.stack) || String(err));
});
