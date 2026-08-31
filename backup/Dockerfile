# freaky-backup — single-stage, zero-build
FROM node:22-slim
WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev --no-audit --no-fund || echo "[build] webtorrent install failed — engine disabled at runtime"
COPY server.js ./
COPY lib ./lib
COPY config ./config
EXPOSE 8101 8102
# default: English instance; run a second container for Tamil (see docker-compose.yml)
CMD ["node", "server.js", "--config", "config/english.json"]
