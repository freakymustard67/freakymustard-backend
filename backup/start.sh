#!/usr/bin/env bash
# freaky-backup · start both instances in the background
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p logs

if [ ! -d node_modules ]; then
  echo "[start] installing dependencies (webtorrent for the direct-stream engine)…"
  npm install --no-audit --no-fund
fi

start_one() {
  local name="$1" cfg="$2" port="$3"
  if curl -sf -m 2 "http://127.0.0.1:${port}/healthz" >/dev/null 2>&1; then
    echo "[start] ${name} already up on :${port}"
    return
  fi
  setsid nohup node server.js --config "$cfg" >> "logs/${name}.log" 2>&1 &
  echo $! > "logs/${name}.pid"
  echo "[start] ${name} launching on :${port} (pid $(cat "logs/${name}.pid"), log logs/${name}.log)"
}

start_one english config/english.json 8101
start_one tamil   config/tamil.json   8102

sleep 1
echo "[start] done — landing pages:"
echo "        http://127.0.0.1:8101/  (English)"
echo "        http://127.0.0.1:8102/  (Tamil)"
