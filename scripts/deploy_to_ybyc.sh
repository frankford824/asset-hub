#!/usr/bin/env bash
# 把当前仓同步到 ybyc 的 ASSET_HUB_ROOT 并重启服务
set -euo pipefail

REMOTE="${ASSET_HUB_REMOTE:-ybyc}"
ROOT="${ASSET_HUB_ROOT:-/opt/asset-hub}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

rsync -az --delete \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude 'web/node_modules/' \
  --exclude '__pycache__/' \
  --exclude '.pytest_cache/' \
  "${SRC}/" "${REMOTE}:${ROOT}/"

ssh "${REMOTE}" "sudo chmod 0751 ${ROOT} && sudo ${ROOT}/.venv/bin/pip install -e ${ROOT} -q && sudo systemctl restart asset-hub-api asset-hub-worker"
echo "deployed to ${REMOTE}:${ROOT}"
