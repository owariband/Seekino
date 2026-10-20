#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
state_dir="${project_dir}/outputs/local-services/nginx"
action="${1:-start}"
if (($#)); then shift; fi

if ! command -v nginx >/dev/null 2>&1; then
  echo "找不到 nginx；macOS 可先执行 brew install nginx mkcert。" >&2
  exit 1
fi
case "${action}" in
  stop)
    nginx_pid="$(cat "${state_dir}/nginx.pid")"
    nginx -p "${state_dir}/" -c "${state_dir}/nginx.conf" -s quit
    for ((attempt = 0; attempt < 50; attempt++)); do
      if ! kill -0 "${nginx_pid}" 2>/dev/null; then
        exit 0
      fi
      sleep 0.1
    done
    echo "Nginx 仍在等待已有连接结束；稍后再启动，不会强制终止请求。" >&2
    exit 1
    ;;
  start|check) ;;
  *) echo "用法：$0 [start|check|stop] [额外证书域名或 IP ...]" >&2; exit 1 ;;
esac
if [[ "${action}" == start && -f "${state_dir}/nginx.pid" ]] && \
  kill -0 "$(cat "${state_dir}/nginx.pid")" 2>/dev/null; then
  echo "项目 Nginx 已在运行；需要改配置时先执行 $0 stop。" >&2
  exit 1
fi
for dependency in mkcert python3; do
  if ! command -v "${dependency}" >/dev/null 2>&1; then
    echo "找不到 ${dependency}；macOS 依赖为 nginx、mkcert 和 python3。" >&2
    exit 1
  fi
done
if [[ ! -f "${project_dir}/web/dist/index.html" ]]; then
  echo "请先在 web/ 执行 npm run build。" >&2
  exit 1
fi

umask 077
mkdir -p "${state_dir}/seekino-certs"
# Keep this project's CA isolated. Installing it into an OS trust store is manual.
export CAROOT="${state_dir}/seekino-ca"
bind="${ANIVAG_HTTPS_BIND:-127.0.0.1}"
port="${ANIVAG_HTTPS_PORT:-8443}"
api_port="${ANIVAG_HTTP_PORT:-9621}"
config_path="${state_dir}/nginx.conf"
if [[ "${action}" == check ]]; then
  config_path="$(mktemp "${state_dir}/nginx-check-XXXXXX")"
  trap 'rm -f "${config_path}"' EXIT
fi
python3 - "${project_dir}" "${config_path}" "${bind}" "${port}" "${api_port}" <<'PY'
import ipaddress
from pathlib import Path
import sys

project, config_path = map(Path, sys.argv[1:3])
bind, port, api_port = sys.argv[3:]
address = ipaddress.IPv4Address(bind)
if address.is_unspecified or address.is_multicast:
    raise SystemExit("ANIVAG_HTTPS_BIND 请使用 127.0.0.1 或本机具体的内网 IPv4 地址")
if any(not value.isascii() or not value.isdigit() or not 1024 <= int(value) <= 65535
       for value in (port, api_port)):
    raise SystemExit("本地 HTTPS 和 API 端口必须在 1024–65535 之间")
values = {"BIND": str(address), "PORT": port, "API_PORT": api_port,
          "WEB_ROOT": str(project / "web/dist").replace("\\", "\\\\").replace('"', '\\"')}
config = (project / "deploy/nginx/nginx.conf.template").read_text()
for key, value in values.items():
    config = config.replace(f"@{key}@", value)
config_path.write_text(config)
PY

if [[ "${action}" == start ]]; then
  if [[ "${bind}" != 127.0.0.1 ]]; then
    python3 - "${api_port}" <<'PY'
import sys
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener

try:
    with build_opener(ProxyHandler({})).open(
        f"http://127.0.0.1:{sys.argv[1]}/api/auth/me", timeout=3,
    ):
        raise SystemExit("拒绝开放内网：目标 API 无凭证即可访问，可能是 local 管理员实例。请指定 cloud API 端口。")
except HTTPError as error:
    if error.code != 401:
        raise SystemExit("目标 API 身份检查未通过，暂不开放内网。") from None
except (OSError, URLError):
    raise SystemExit("目标 cloud API 未就绪，暂不开放内网。") from None
PY
  fi
  "${project_dir}/deploy/nginx/certificates.sh" "${bind}" "$@"
fi
nginx -p "${state_dir}/" -c "${config_path}" -t
if [[ "${action}" == start ]]; then
  nginx -p "${state_dir}/" -c "${state_dir}/nginx.conf"
  echo "[就绪] https://${bind}:${port}"
  if [[ "${bind}" == 127.0.0.1 ]]; then
    echo "[本机] https://localhost:${port}"
  fi
  echo "[证书] 信任步骤见 deploy/nginx/README.md；根证书：${CAROOT}/rootCA.pem"
fi
