#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
state_dir="${project_dir}/outputs/local-services/elasticsearch"
version=8.19.22
es_port="${SEEKINO_ELASTICSEARCH_PORT:-9200}"
es_url="http://127.0.0.1:${es_port}"

elasticsearch_ready() {
  local response health
  response="$(curl --silent --fail --include --noproxy '*' --connect-timeout 1 \
    --max-time 2 "${es_url}")" || return 1
  [[ "${response}" =~ [Xx]-[Ee]lastic-[Pp]roduct:[[:space:]]*Elasticsearch ]] || return 1
  [[ "${response}" =~ \"number\"[[:space:]]*:[[:space:]]*\"8\.19\.[0-9]+\" ]] || return 1
  health="$(curl --silent --fail --noproxy '*' --connect-timeout 1 --max-time 3 \
    "${es_url}/_cluster/health?wait_for_status=yellow&timeout=1s&filter_path=status,timed_out")" || return 1
  [[ "${health}" =~ \"timed_out\"[[:space:]]*:[[:space:]]*false ]] && \
    [[ "${health}" =~ \"status\"[[:space:]]*:[[:space:]]*\"(yellow|green)\" ]]
}

if [[ ! "${es_port}" =~ ^[0-9]+$ ]] || ((es_port < 1 || es_port > 65535)); then
  echo "SEEKINO_ELASTICSEARCH_PORT 必须在 1–65535 之间。" >&2
  exit 1
fi
if elasticsearch_ready; then
  echo "[复用] 已有 Elasticsearch 8.19 / yellow 或 green 集群 ${es_url}；项目启动时仍会校验索引合同。"
  exit 0
fi
if nc -z -w 2 127.0.0.1 "${es_port}" 2>/dev/null; then
  echo "端口 ${es_port} 已占用但 Elasticsearch HTTP 未就绪；不会替换现有进程。" >&2
  exit 1
fi
if [[ "$(uname -s)" != Darwin ]]; then
  echo "此脚本用于原生 macOS；其它环境请使用根目录 Compose 的 elasticsearch profile。" >&2
  exit 1
fi
case "$(uname -m)" in
  arm64) architecture=aarch64 ;;
  x86_64) architecture=x86_64 ;;
  *) echo "不支持的 macOS 架构。" >&2; exit 1 ;;
esac

distribution="${state_dir}/distribution/elasticsearch-${version}"
mkdir -p "${state_dir}/downloads" "${state_dir}/distribution" "${state_dir}/data" "${state_dir}/logs"
if [[ ! -f "${distribution}/.seekino-installed" || ! -x "${distribution}/bin/elasticsearch" ]]; then
  archive="elasticsearch-${version}-darwin-${architecture}.tar.gz"
  download_url="https://artifacts.elastic.co/downloads/elasticsearch/${archive}"
  echo "[下载] Elasticsearch ${version} / ${architecture}（含 JDK）"
  if [[ ! -f "${state_dir}/downloads/${archive}" ]]; then
    curl --fail --location --retry 3 --connect-timeout 15 \
      --output "${state_dir}/downloads/${archive}.part" "${download_url}"
    mv "${state_dir}/downloads/${archive}.part" "${state_dir}/downloads/${archive}"
  fi
  curl --fail --location --retry 3 --connect-timeout 15 \
    --output "${state_dir}/downloads/${archive}.sha512" "${download_url}.sha512"
  (cd "${state_dir}/downloads" && shasum -a 512 -c "${archive}.sha512")
  install_dir="$(mktemp -d "${state_dir}/distribution/.install-XXXXXX")"
  tar -xzf "${state_dir}/downloads/${archive}" -C "${install_dir}"
  touch "${install_dir}/elasticsearch-${version}/.seekino-installed"
  if [[ -e "${distribution}" ]]; then
    previous_dir="${distribution}.previous-$(date +%Y%m%d%H%M%S)"
    if [[ -e "${previous_dir}" ]]; then
      echo "安装备份路径已存在；保留现有程序与暂存目录，请检查 ${install_dir}。" >&2
      exit 1
    fi
    mv "${distribution}" "${previous_dir}"
    echo "[保留] 原程序目录 ${previous_dir}"
  fi
  mv "${install_dir}/elasticsearch-${version}" "${distribution}"
  rmdir "${install_dir}"
fi

# Only this project's loopback node is started. No application, migration or rebuild.
launcher_python="${project_dir}/.venv/bin/python"
if [[ ! -x "${launcher_python}" ]]; then
  launcher_python="$(command -v python3)"
fi
# Match the project's existing detached subprocess boundary, not this command's session.
ES_JAVA_OPTS="${ES_JAVA_OPTS:--Xms512m -Xmx512m}" \
  "${launcher_python}" -c '
import subprocess, sys
with open(sys.argv[1], "ab") as log:
    subprocess.run(sys.argv[2:], stdin=subprocess.DEVNULL, stdout=log,
                   stderr=subprocess.STDOUT, start_new_session=True,
                   check=True, timeout=120)
' "${state_dir}/console.log" \
  "${distribution}/bin/elasticsearch" -d -p "${state_dir}/elasticsearch.pid" \
  -Ecluster.name=seekino-local -Enode.name=seekino-local-es \
  -Enetwork.host=127.0.0.1 -Ehttp.port="${es_port}" -Etransport.port=0 \
  -Ediscovery.type=single-node -Expack.security.enabled=false \
  -Expack.security.autoconfiguration.enabled=false -Expack.ml.enabled=false \
  -Eingest.geoip.downloader.enabled=false \
  -Epath.data="${state_dir}/data" -Epath.logs="${state_dir}/logs"

for ((attempt = 0; attempt < 60; attempt++)); do
  if elasticsearch_ready; then
    echo "[就绪] Elasticsearch ${es_url}；日志 outputs/local-services/elasticsearch/logs/"
    exit 0
  fi
  sleep 1
done
echo "Elasticsearch 未在限定时间内就绪；请检查 ${state_dir}/logs/。" >&2
exit 1
