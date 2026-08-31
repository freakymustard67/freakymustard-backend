#!/usr/bin/env bash
# freaky-backup · stop both instances
set -u
cd "$(dirname "$0")"
for name in english tamil; do
  pidfile="logs/${name}.pid"
  if [ -f "$pidfile" ]; then
    pid="$(cat "$pidfile")"
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" && echo "[stop] ${name} (pid ${pid}) signalled"
    else
      echo "[stop] ${name} not running (stale pid)"
    fi
    rm -f "$pidfile"
  else
    # fallback: kill by config path match
    pkill -f "server.js --config config/${name}.json" 2>/dev/null && echo "[stop] ${name} killed by pattern" || echo "[stop] ${name} not running"
  fi
done
