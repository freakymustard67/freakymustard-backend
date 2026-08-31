#!/usr/bin/env bash
set -e
# Runs backup-english :8101, backup-tamil :8102, and the FastAPI backend :7860
# inside the single HF Space container. Logs go to stdout so HF captures them.

echo "[start] launching freaky-backup English :8101"
node ./backup/server.js --config ./backup/config/english.json > /tmp/backup-english.log 2>&1 &
echo "[start] launching freaky-backup Tamil :8102"
node ./backup/server.js --config ./backup/config/tamil.json > /tmp/backup-tamil.log 2>&1 &
sleep 2
echo "[start] backup English log tail:"
tail -n 5 /tmp/backup-english.log || true
echo "[start] backup Tamil log tail:"
tail -n 5 /tmp/backup-tamil.log || true

echo "[start] launching backend :7860"
exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-7860} --app-dir /app
