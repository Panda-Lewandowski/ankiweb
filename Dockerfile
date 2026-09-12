# Verified multi-platform registry digests, 2026-09-12.
FROM node:25.3.0-bookworm-slim@sha256:ee036ac49abf78c38be1ae0a622fb9aa2beeffe4d7b65eeca6fd2a90b685726d AS frontend
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci
COPY shell_src ./shell_src
COPY tools/build_shell.mjs ./tools/build_shell.mjs
RUN npm run build
COPY web/package.json web/package-lock.json ./web/
RUN npm --prefix web ci
COPY web ./web
RUN VITE_HIDE_LEGACY=true npm --prefix web run build

FROM ghcr.io/astral-sh/uv:0.11.29@sha256:eb2843a1e56fd9e30c7276ce1a52cba86e64c7b385f5e3279a0e08e02dd058fc AS uv
FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2 AS backend
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY ankiweb ./ankiweb
COPY tools ./tools
COPY LICENSE THIRD-PARTY-NOTICES.md UPSTREAM.md README.md ./
COPY LICENSES ./LICENSES
RUN uv sync --frozen --no-dev
RUN uv pip install --python .venv/bin/python pip==25.3 && .venv/bin/python tools/fetch_web_assets.py

FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2 AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 ca-certificates && apt-get clean
WORKDIR /app
ENV PATH=/app/.venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    ANKIWEB_PRODUCTION=true ANKIWEB_HOST=0.0.0.0 ANKIWEB_PORT=8000 \
    ANKIWEB_AC_ENABLED=false ANKIWEB_COLLECTION=/data/anki/current/collection.anki2 \
    ANKIWEB_BACKUPS=/data/backups ANKIWEB_AUTH_DB=/data/anki/auth.sqlite3
COPY --from=backend /app /app
COPY --from=frontend /build/ankiweb/shell/static /app/ankiweb/shell/static
COPY --from=frontend /build/ankiweb/trainer_static /app/ankiweb/trainer_static
RUN groupadd --gid 10001 trainer && useradd --uid 10001 --gid trainer --no-create-home trainer \
    && mkdir -p /data/anki /data/backups && chown -R trainer:trainer /data
USER 10001:10001
VOLUME ["/data/anki", "/data/backups"]
EXPOSE 8000
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=8)"
CMD ["python", "-m", "ankiweb.production"]
