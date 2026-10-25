#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source_branch=main
if [[ "${1:-}" == --branch ]]; then
  if [[ -z "${2:-}" || "${2}" == -* ]]; then
    echo "用法：./start.sh [--branch 分支名] [start|status|stop]" >&2
    exit 1
  fi
  source_branch="$2"
  shift 2
fi
source_dir=""
worktree_dir=""
while IFS= read -r -d '' entry; do
  case "${entry}" in
    'worktree '*) worktree_dir="${entry#worktree }" ;;
    "branch refs/heads/${source_branch}")
      if [[ -n "${source_dir}" ]]; then
        echo "存在多个 ${source_branch} 工作区，无法确定启动来源。" >&2
        exit 1
      fi
      source_dir="${worktree_dir}"
      ;;
  esac
done < <(git -C "${project_dir}" worktree list --porcelain -z)
if [[ -z "${source_dir}" || ! -f "${source_dir}/start.sh" ]]; then
  echo "未找到已检出 ${source_branch} 分支的工作区；请先准备 ${source_branch} 工作区。" >&2
  exit 1
fi
if [[ "${source_branch}" != main ]]; then
  set -- --branch "${source_branch}" "$@"
fi
if [[ "${project_dir}" != "${source_dir}" ]]; then
  echo "[${source_branch}] 使用工作区 ${source_dir}"
  exec bash "${source_dir}/start.sh" "$@"
fi
cd "${project_dir}"

if ! command -v uv >/dev/null 2>&1; then
  echo "找不到 uv，请先安装 uv。" >&2
  exit 1
fi

# Preserve the executable outside the shell-variable namespace. The dotenv
# parser below may legitimately return any valid environment variable name.
set -- "$(command -v uv)" "$@"

if [[ ! -x .venv/bin/python ]]; then
  "$1" sync
fi

if [[ -f .env.local ]]; then
  env_names="$(
    .venv/bin/python scripts/local_services.py _env-names --env-file .env.local
  )"
  while IFS= read -r env_name; do
    if [[ -n "${env_name}" ]]; then
      unset -v "${env_name}" || true
    fi
  done <<< "${env_names}"
  unset -v env_name env_names
  exec "$1" run --no-sync --env-file .env.local python scripts/local_services.py "${@:2}"
fi

exec "$1" run --no-sync python scripts/local_services.py "${@:2}"
