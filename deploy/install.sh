#!/usr/bin/env bash
# asset-hub 默认安装到 ybyc 环境（路径可用环境变量覆盖）
set -euo pipefail

ASSET_HUB_ROOT="${ASSET_HUB_ROOT:-/opt/asset-hub}"
ASSET_HUB_VENV="${ASSET_HUB_VENV:-${ASSET_HUB_ROOT}/.venv}"
ASSET_HUB_DATA="${ASSET_HUB_DATA:-/var/lib/asset-hub}"
ASSET_HUB_CONFIG="${ASSET_HUB_CONFIG:-/etc/asset-hub/config.yaml}"
ASSET_HUB_LIBRARY="${ASSET_HUB_LIBRARY:-/home/resourse}"
ASSET_HUB_LOG="${ASSET_HUB_LOG:-/var/log/asset-hub}"
RUN_USER="${ASSET_HUB_USER:-ybyc}"
RUN_GROUP="${ASSET_HUB_GROUP:-asset-hub}"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKIP_APT="${SKIP_APT:-0}"
SKIP_NGINX="${SKIP_NGINX:-0}"
SKIP_SYSTEMD="${SKIP_SYSTEMD:-0}"
SKIP_SYNC_CODE="${SKIP_SYNC_CODE:-0}"

log() { printf '[install] %s\n' "$*"; }
die() { printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }

need_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    die "请用 root 运行：sudo ASSET_HUB_ROOT=... $0"
  fi
}

ensure_user_group() {
  if ! getent group "${RUN_GROUP}" >/dev/null; then
    log "创建组 ${RUN_GROUP}"
    groupadd --system "${RUN_GROUP}"
  fi
  if ! id -u "${RUN_USER}" >/dev/null 2>&1; then
    log "创建用户 ${RUN_USER}"
    useradd --system --create-home --shell /usr/sbin/nologin \
      --gid "${RUN_GROUP}" "${RUN_USER}" || \
      useradd --system --create-home --shell /bin/bash \
        --gid "${RUN_GROUP}" "${RUN_USER}"
  else
    usermod -a -G "${RUN_GROUP}" "${RUN_USER}" || true
  fi
  # nginx 读 X-Accel 文件需要组可读
  if id -u www-data >/dev/null 2>&1; then
    usermod -a -G "${RUN_GROUP}" www-data || true
  fi
}

install_deps() {
  if [[ "${SKIP_APT}" == "1" ]]; then
    log "跳过 apt 依赖（SKIP_APT=1）"
    return
  fi
  if ! command -v apt-get >/dev/null 2>&1; then
    log "非 apt 系统，跳过包安装；请自行保证 python3.12/venv/nginx/sqlite3"
    return
  fi
  log "安装系统依赖"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq \
    python3 \
    python3-venv \
    python3-pip \
    sqlite3 \
    nginx \
    rsync \
    curl \
    acl
}

sync_code() {
  if [[ "${SKIP_SYNC_CODE}" == "1" ]]; then
    log "跳过代码同步（SKIP_SYNC_CODE=1）；假定 ${ASSET_HUB_ROOT} 已就绪"
    [[ -d "${ASSET_HUB_ROOT}" ]] || die "${ASSET_HUB_ROOT} 不存在"
  else
    log "同步代码 ${REPO_DIR} → ${ASSET_HUB_ROOT}"
    mkdir -p "${ASSET_HUB_ROOT}"
    rsync -a --delete \
      --exclude '.git/' \
      --exclude '.venv/' \
      --exclude 'web/node_modules/' \
      --exclude '__pycache__/' \
      --exclude '*.pyc' \
      --exclude '.pytest_cache/' \
      "${REPO_DIR}/" "${ASSET_HUB_ROOT}/"
  fi
  # nginx 静态站点必须能穿越代码根目录；0751 不开放目录列表。
  chmod 751 "${ASSET_HUB_ROOT}"
}

make_dirs() {
  log "创建数据/配置目录"
  mkdir -p \
    "${ASSET_HUB_DATA}/db" \
    "${ASSET_HUB_DATA}/finalized" \
    "${ASSET_HUB_DATA}/archive" \
    "${ASSET_HUB_DATA}/jobs" \
    "${ASSET_HUB_DATA}/tmp" \
    "$(dirname "${ASSET_HUB_CONFIG}")" \
    "${ASSET_HUB_LOG}"

  if [[ ! -d "${ASSET_HUB_LIBRARY}" ]]; then
    log "警告: 素材库 ${ASSET_HUB_LIBRARY} 不存在（徐凯旁路后续挂载即可）"
  fi

  chown -R "${RUN_USER}:${RUN_GROUP}" "${ASSET_HUB_DATA}" "${ASSET_HUB_LOG}"
  chmod 2775 "${ASSET_HUB_DATA}" \
    "${ASSET_HUB_DATA}/db" \
    "${ASSET_HUB_DATA}/finalized" \
    "${ASSET_HUB_DATA}/archive" \
    "${ASSET_HUB_DATA}/jobs" \
    "${ASSET_HUB_DATA}/tmp"
  # 组可读，供 www-data X-Accel
  chmod -R g+rX "${ASSET_HUB_DATA}"
}

install_venv() {
  log "创建/更新 venv: ${ASSET_HUB_VENV}"
  if [[ ! -d "${ASSET_HUB_VENV}" ]]; then
    python3 -m venv "${ASSET_HUB_VENV}"
  fi
  # shellcheck disable=SC1091
  source "${ASSET_HUB_VENV}/bin/activate"
  pip install -U pip setuptools wheel -q
  pip install -e "${ASSET_HUB_ROOT}" -q
  chown -R "${RUN_USER}:${RUN_GROUP}" "${ASSET_HUB_VENV}"
  chown -R "${RUN_USER}:${RUN_GROUP}" "${ASSET_HUB_ROOT}"
}

install_config() {
  if [[ -f "${ASSET_HUB_CONFIG}" ]]; then
    log "保留已有配置 ${ASSET_HUB_CONFIG}"
  else
    log "写入默认配置 ${ASSET_HUB_CONFIG}"
    cp "${ASSET_HUB_ROOT}/configs/asset-hub.example.yaml" "${ASSET_HUB_CONFIG}"
    # 按实际路径改写
    sed -i \
      -e "s|^library_root:.*|library_root: ${ASSET_HUB_LIBRARY}|" \
      -e "s|^data_root:.*|data_root: ${ASSET_HUB_DATA}|" \
      "${ASSET_HUB_CONFIG}"
  fi
  chown root:"${RUN_GROUP}" "${ASSET_HUB_CONFIG}"
  chmod 640 "${ASSET_HUB_CONFIG}"
}

render_unit() {
  local src="$1"
  local dst="$2"
  sed \
    -e "s|/opt/asset-hub|${ASSET_HUB_ROOT}|g" \
    -e "s|/var/lib/asset-hub|${ASSET_HUB_DATA}|g" \
    -e "s|/etc/asset-hub/config.yaml|${ASSET_HUB_CONFIG}|g" \
    -e "s|User=ybyc|User=${RUN_USER}|g" \
    -e "s|Group=asset-hub|Group=${RUN_GROUP}|g" \
    "${src}" > "${dst}"
}

install_systemd() {
  if [[ "${SKIP_SYSTEMD}" == "1" ]]; then
    log "跳过 systemd（SKIP_SYSTEMD=1）"
    return
  fi
  if ! command -v systemctl >/dev/null 2>&1; then
    log "无 systemctl，跳过单元安装"
    return
  fi
  log "安装 systemd 单元"
  local unit
  for unit in asset-hub-library-mount.service asset-hub-library-mount.timer \
              asset-hub-api.service asset-hub-worker.service \
              asset-hub-sync.service asset-hub-sync.timer \
              asset-hub-index.service asset-hub-index.timer; do
    render_unit "${ASSET_HUB_ROOT}/deploy/systemd/${unit}" \
      "/etc/systemd/system/${unit}"
  done
  systemctl daemon-reload
  systemctl enable asset-hub-library-mount.service asset-hub-library-mount.timer \
    asset-hub-api.service asset-hub-worker.service \
    asset-hub-sync.timer asset-hub-index.timer
  systemctl restart asset-hub-library-mount.service || true
  systemctl restart asset-hub-library-mount.timer || true
  systemctl restart asset-hub-api.service asset-hub-worker.service || true
  systemctl start asset-hub-sync.timer asset-hub-index.timer || true
}

install_nginx() {
  if [[ "${SKIP_NGINX}" == "1" ]]; then
    log "跳过 nginx（SKIP_NGINX=1）"
    return
  fi
  if ! command -v nginx >/dev/null 2>&1; then
    log "未安装 nginx，跳过站点"
    return
  fi
  log "安装 nginx 站点"
  local conf_src="${ASSET_HUB_ROOT}/deploy/nginx/asset-hub.conf"
  local conf_dst="/etc/nginx/sites-available/asset-hub"
  sed \
    -e "s|__ASSET_HUB_ROOT__|${ASSET_HUB_ROOT}|g" \
    -e "s|__ASSET_HUB_DATA__|${ASSET_HUB_DATA}|g" \
    "${conf_src}" > "${conf_dst}"

  mkdir -p "${ASSET_HUB_ROOT}/web/dist"
  if [[ ! -f "${ASSET_HUB_ROOT}/web/dist/index.html" ]]; then
    cat > "${ASSET_HUB_ROOT}/web/dist/index.html" <<'HTML'
<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>asset-hub</title></head>
<body>
  <h1>asset-hub</h1>
  <p>前端尚未构建。API: <a href="/health">/health</a></p>
</body>
</html>
HTML
    chown -R "${RUN_USER}:${RUN_GROUP}" "${ASSET_HUB_ROOT}/web"
  fi

  ln -sfn "${conf_dst}" /etc/nginx/sites-enabled/asset-hub
  # 避免与默认站点抢 80
  if [[ -e /etc/nginx/sites-enabled/default ]]; then
    rm -f /etc/nginx/sites-enabled/default
  fi
  nginx -t
  systemctl reload nginx || systemctl restart nginx || true
}

print_summary() {
  cat <<EOF

======== asset-hub 安装完成 ========
  ASSET_HUB_ROOT   = ${ASSET_HUB_ROOT}
  ASSET_HUB_VENV   = ${ASSET_HUB_VENV}
  ASSET_HUB_DATA   = ${ASSET_HUB_DATA}
  ASSET_HUB_CONFIG = ${ASSET_HUB_CONFIG}
  ASSET_HUB_LIBRARY= ${ASSET_HUB_LIBRARY}
  RUN_USER/GROUP   = ${RUN_USER}:${RUN_GROUP}

  健康检查: curl -s http://127.0.0.1/health
  日志:     journalctl -u asset-hub-api -f
====================================
EOF
}

main() {
  need_root
  log "REPO_DIR=${REPO_DIR}"
  install_deps
  ensure_user_group
  sync_code
  make_dirs
  install_venv
  install_config
  install_systemd
  install_nginx
  print_summary
}

main "$@"
