# syntax=docker/dockerfile:1
FROM node:24-bookworm-slim AS web-build
WORKDIR /build
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS api
COPY --from=ghcr.io/astral-sh/uv:0.11.24 /uv /uvx /usr/local/bin/
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-editable \
    && python -c 'from codex_cli_bin import bundled_codex_path; from pathlib import Path; Path("/usr/local/bin/codex").symlink_to(bundled_codex_path())'
COPY agent/ ./agent/
COPY engine/ ./engine/
COPY gateway/ ./gateway/
COPY server/ ./server/
COPY config/ ./config/
COPY config.yaml ./
RUN useradd --create-home --uid 10001 seekino \
    && mkdir -p /data && chown seekino:seekino /data
USER seekino
EXPOSE 9621
CMD ["python", "-m", "server.main"]

FROM nginx:1.28-alpine AS web
COPY --from=web-build /build/dist/ /usr/share/nginx/html/
COPY deploy/docker/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
