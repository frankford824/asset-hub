#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "请使用 sudo 运行本脚本" >&2
  exit 1
fi

ROOT=/opt/asset-hub
stamp="$(date +%Y%m%d-%H%M%S)"
backup="/var/backups/asset-hub/${stamp}"
mkdir -p "${backup}"
cp -a /etc/nginx/sites-available/asset-hub "${backup}/nginx.asset-hub"
cp -a /etc/systemd/system/asset-hub-api.service "${backup}/asset-hub-api.service"
cp -a /etc/asset-hub/config.yaml "${backup}/config.yaml"
if [[ -f /etc/sudoers.d/asset-hub-ybyc ]]; then
  cp -a /etc/sudoers.d/asset-hub-ybyc "${backup}/sudoers.asset-hub-ybyc"
fi

sudoers_tmp="$(mktemp)"
rendered_nginx="$(mktemp)"
cleanup() {
  rm -f -- "${sudoers_tmp}" "${rendered_nginx}"
}
trap cleanup EXIT

printf '%s\n' \
  'Cmnd_Alias ASSET_HUB_OPERATIONS = /usr/bin/systemctl daemon-reload, /usr/bin/systemctl reload nginx.service, /usr/bin/systemctl restart asset-hub-api.service, /usr/bin/systemctl restart asset-hub-worker.service, /usr/bin/systemctl restart asset-hub-api.service asset-hub-worker.service, /usr/sbin/nginx -t' \
  'ybyc ALL=(root) NOPASSWD: ASSET_HUB_OPERATIONS' \
  >"${sudoers_tmp}"
chmod 0440 "${sudoers_tmp}"
visudo -cf "${sudoers_tmp}"
install -o root -g root -m 0440 "${sudoers_tmp}" /etc/sudoers.d/asset-hub-ybyc
visudo -cf /etc/sudoers.d/asset-hub-ybyc

sed \
  -e 's|__ASSET_HUB_ROOT__|/opt/asset-hub|g' \
  -e 's|__ASSET_HUB_DATA__|/var/lib/asset-hub|g' \
  "${ROOT}/deploy/nginx/asset-hub.conf" >"${rendered_nginx}"
install -o root -g root -m 0644 \
  "${rendered_nginx}" /etc/nginx/sites-available/asset-hub
install -o root -g root -m 0644 \
  "${ROOT}/deploy/systemd/asset-hub-api.service" \
  /etc/systemd/system/asset-hub-api.service

if ! grep -q '^job_retention_hours:' /etc/asset-hub/config.yaml; then
  printf '\njob_retention_hours: 24\n' >>/etc/asset-hub/config.yaml
fi
chown root:asset-hub /etc/asset-hub/config.yaml
chmod 0640 /etc/asset-hub/config.yaml

nginx -t
systemctl daemon-reload
systemctl reload nginx.service
systemctl restart asset-hub-api.service asset-hub-worker.service
systemctl is-active nginx.service asset-hub-api.service asset-hub-worker.service
printf 'backup=%s\n' "${backup}"
