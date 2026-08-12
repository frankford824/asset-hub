# asset-hub

ybyc 局域网素材中台：本地 catalog / 后台 sync 预热 / 极速打包下载。

**铁律：** 业务热路径（检索 / 打包 / 下载）只读 NVMe 本地缓存，禁止打 OSS/公网。

## 仓内结构

```text
asset-hub/
  deploy/          # install.sh、systemd、nginx 模板
  configs/         # 示例配置
  src/asset_hub/   # API / Worker / Sync / Catalog / Pack
  web/dist/        # 前端静态页（nginx 直出）
  scripts/         # 发布与压测脚本
  tests/
```

## 默认运行路径（ybyc）

| 用途 | 默认 | 覆盖变量 |
|---|---|---|
| 代码发布 | `/opt/asset-hub` | `ASSET_HUB_ROOT` |
| 数据（NVMe） | `/var/lib/asset-hub` | `ASSET_HUB_DATA` |
| 配置 | `/etc/asset-hub/config.yaml` | `ASSET_HUB_CONFIG` |
| 徐凯素材库 | `/home/resourse` | `ASSET_HUB_LIBRARY` |

## 安装（在 ybyc）

```bash
export ASSET_HUB_ROOT=/opt/asset-hub   # 可选，默认即此
sudo ./deploy/install.sh
# 或从开发机：
./scripts/deploy_to_ybyc.sh
```

访问：`http://<ybyc-lan-ip>/`（nginx → API `127.0.0.1:8080`）。

首次建议：

```bash
sudo systemctl start asset-hub-sync.service   # mock 预热 finalized
sudo systemctl start asset-hub-index.service  # 索引 /home/resourse
curl -s http://127.0.0.1/health
./scripts/bench_lan.sh
```

## Provider

配置 `provider: mock`（默认）或后续 `http`。线上 OSS/清单 API **不在本仓实现**；你提供接口后只改 `http.base_url` / `token`。

## 数据分层

| kind | 路径 | 用途 |
|---|---|---|
| `finalized` | `$DATA/finalized` | 生产终稿热缓存（打包默认） |
| `library` | `/home/resourse` | 徐凯冷库，检索/旁路下载 |
| `archive` | `$DATA/archive` | P4 历史冷库（`sync.kinds` 加入 `archive` 才启用） |

## 本地测试

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e . pytest
ASSET_HUB_CONFIG=/dev/null pytest -q
```
