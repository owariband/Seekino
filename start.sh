#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "${project_dir}"

if ! command -v uv >/dev/null 2>&1; then
  echo "找不到 uv，请先安装 uv。" >&2
  exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
  uv sync
fi

if [[ -f .env.local ]]; then
  unset ARK_API_KEY DEEPSEEK_API_KEY
  exec uv run --no-sync --env-file .env.local python scripts/local_services.py "$@"
fi

exec uv run --no-sync python scripts/local_services.py "$@"
