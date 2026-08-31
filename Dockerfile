# Streamda Proxy + Freaky Backup — HF Spaces Docker image
# Single container runs: backup-english :8101, backup-tamil :8102, backend :7860
# Read: https://huggingface.co/docs/hub/spaces-sdks-docker
FROM python:3.12-slim

# — Node 22 for the backup sidecars (freaky-backup) —
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
  && mkdir -p /etc/apt/keyrings \
  && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
  && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" > /etc/apt/sources.list.d/nodesource.list \
  && apt-get update && apt-get install -y --no-install-recommends nodejs \
  && rm -rf /var/lib/apt/lists/*

# HF requirement: user 1000
RUN useradd -m -u 1000 user
WORKDIR /app

# — Python deps —
COPY --chown=user ./requirements.txt requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
  && pip install --no-cache-dir -r requirements.txt

# — Backup sidecars (vendored freaky-backup) —
COPY --chown=user ./backup ./backup
RUN cd ./backup && npm install --omit=dev --no-audit --no-fund || echo "[build] backup npm install failed — engine will be disabled at runtime"

# — Backend app —
COPY --chown=user ./app /app

# — Startup: run both sidecars + backend in one container —
COPY --chown=user ./start.sh ./start.sh
RUN chmod +x ./start.sh

USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PORT=7860 \
    BACKUP_ENGLISH=http://127.0.0.1:8101 \
    BACKUP_TAMIL=http://127.0.0.1:8102 \
    PUBLIC_BASE_URL=https://freakymustard67-potato.hf.space

EXPOSE 7860 8101 8102

CMD ["./start.sh"]
