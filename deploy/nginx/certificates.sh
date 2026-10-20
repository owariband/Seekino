#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
state_dir="${project_dir}/outputs/local-services/nginx"
export CAROOT="${state_dir}/seekino-ca"
cert_dir="${state_dir}/seekino-certs"

for dependency in openssl mkcert python3; do
  if ! command -v "${dependency}" >/dev/null 2>&1; then
    echo "找不到 ${dependency}；请安装 OpenSSL、mkcert 和 Python 3。" >&2
    exit 1
  fi
done
umask 077
mkdir -p "${state_dir}" "${cert_dir}"
if [[ ! -d "${CAROOT}" ]]; then
  temporary_ca="$(mktemp -d "${state_dir}/.seekino-ca-XXXXXX")"
  trap 'rm -f "${temporary_ca}/rootCA.pem" "${temporary_ca}/rootCA-key.pem"; rmdir "${temporary_ca}" 2>/dev/null || true' EXIT
  openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 \
    -out "${temporary_ca}/rootCA-key.pem" 2>/dev/null
  openssl req -new -x509 -sha256 -days 3650 \
    -key "${temporary_ca}/rootCA-key.pem" -out "${temporary_ca}/rootCA.pem" \
    -config <(cat <<'CONFIG'
[req]
prompt = no
distinguished_name = identity
x509_extensions = ca
[identity]
O = Seekino
OU = Local Development
CN = Seekino Local Development CA
[ca]
basicConstraints = critical,CA:true,pathlen:0
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
CONFIG
)
  chmod 400 "${temporary_ca}/rootCA-key.pem"
  chmod 644 "${temporary_ca}/rootCA.pem"
  mv "${temporary_ca}" "${CAROOT}"
  trap - EXIT
fi
if [[ ! -f "${CAROOT}/rootCA.pem" || ! -f "${CAROOT}/rootCA-key.pem" ]]; then
  echo "Seekino CA 文件不完整；保留现场，不覆盖已有证书或私钥。" >&2
  exit 1
fi
subject="$(openssl x509 -in "${CAROOT}/rootCA.pem" -noout -subject -nameopt RFC2253)"
subject="${subject#subject=}"
subject="${subject# }"
if [[ "${subject}" != 'CN=Seekino Local Development CA,OU=Local Development,O=Seekino' ]]; then
  echo "CA 名称不符合预期；不会复用或覆盖它：${subject}" >&2
  exit 1
fi
python3 - "${cert_dir}/site-request.conf" "$@" <<'PY'
import ipaddress
from pathlib import Path
import re
import sys

lines = ["[req]", "prompt=no", "distinguished_name=identity", "req_extensions=site",
         "[identity]", "O=Seekino", "OU=Local Development", "CN=Seekino Local HTTPS",
         "[site]", "subjectAltName=@hosts", "[hosts]"]
for index, host in enumerate(dict.fromkeys(["localhost", "127.0.0.1", "::1", *sys.argv[2:]])):
    try:
        value, kind = str(ipaddress.ip_address(host)), "IP"
    except ValueError:
        if not re.fullmatch(r"(?:\*\.)?[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
            raise SystemExit(f"无效的证书地址：{host!r}")
        value, kind = host, "DNS"
    lines.append(f"{kind}.{index}={value}")
Path(sys.argv[1]).write_text("\n".join(lines) + "\n")
PY
openssl req -new -newkey rsa:2048 -nodes -keyout "${cert_dir}/site-key.pem" \
  -out "${cert_dir}/site.csr" -config "${cert_dir}/site-request.conf" 2>/dev/null
mkcert -cert-file "${cert_dir}/site.pem" -csr "${cert_dir}/site.csr"
openssl x509 -in "${CAROOT}/rootCA.pem" -outform DER \
  -out "${state_dir}/Seekino-Local-Dev-CA.cer"
chmod 644 "${state_dir}/Seekino-Local-Dev-CA.cer"
echo "[CA] Seekino Local Development CA；只生成文件，不安装系统信任。"
