#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
main_dir=""
worktree_dir=""
while IFS= read -r -d '' entry; do
  case "${entry}" in
    'worktree '*) worktree_dir="${entry#worktree }" ;;
    'branch refs/heads/main')
      if [[ -n "${main_dir}" ]]; then
        echo "存在多个 main 工作区，无法确定启动来源。" >&2
        exit 1
      fi
      main_dir="${worktree_dir}"
      ;;
  esac
done < <(git -C "${project_dir}" worktree list --porcelain -z)
if [[ -z "${main_dir}" || ! -f "${main_dir}/start.sh" ]]; then
  echo "未找到已检出 main 分支的工作区；请先准备 main 工作区。" >&2
  exit 1
fi
if [[ "${project_dir}" != "${main_dir}" ]]; then
  echo "[main] 使用工作区 ${main_dir}"
  exec bash "${main_dir}/start.sh" "$@"
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
