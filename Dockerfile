# NexAgent Studio (server edition) — container image for Render, Railway, Fly.io, Hugging Face Spaces or any Docker host.
# Public mode is ON by default here (sign-up + each user's own API keys). Override with NEXAGENT_PUBLIC_MODE=0.

# ---- 1. build the web UI
FROM node:20-alpine AS ui
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npx tsc -b && npx vite build --outDir /ui --emptyOutDir

# ---- 2. runtime
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    NEXAGENT_HOST=0.0.0.0 NEXAGENT_PORT=8600 NEXAGENT_DATA_DIR=/data \
    NEXAGENT_PUBLIC_MODE=1 NEXAGENT_TRUSTED_PROXY=1
WORKDIR /app
COPY requirements.txt requirements.lock.txt ./
RUN pip install -r requirements.lock.txt && pip install -r requirements.txt
COPY backend/ backend/
COPY samples/ samples/
COPY --from=ui /ui backend/nexagent/static
RUN useradd --create-home --uid 10001 nexagent && mkdir -p /data && chmod 1777 /data \
 && printf '%s\n' '#!/bin/sh' \
    '# Started as root (Render/Railway/Docker): make the data disk writable, then drop to the unprivileged user.' \
    '# Started as a non-root user (e.g. Hugging Face Spaces): just run.' \
    'if [ "$(id -u)" = "0" ]; then chown -R nexagent "${NEXAGENT_DATA_DIR:-/data}" 2>/dev/null; exec setpriv --reuid=10001 --regid=10001 --init-groups "$@"; fi' \
    'exec "$@"' > /usr/local/bin/entrypoint && chmod 755 /usr/local/bin/entrypoint
WORKDIR /app/backend
ENTRYPOINT ["/usr/local/bin/entrypoint"]
EXPOSE 8600
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/api/health'%os.environ.get('PORT',os.environ['NEXAGENT_PORT']))"
# Most hosts pass the port in $PORT; NEXAGENT_ORIGIN must be set to the public https URL.
CMD ["sh", "-c", "NEXAGENT_PORT=${PORT:-$NEXAGENT_PORT} exec python -m nexagent.cli serve"]
