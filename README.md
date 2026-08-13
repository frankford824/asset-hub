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
| 既有本地素材目录 | `/home/resourse` | `ASSET_HUB_LIBRARY` |

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
sudo systemctl start asset-hub-sync.service   # 预热 finalized（首次可用 mock 验证）
sudo systemctl start asset-hub-index.service  # 索引 /home/resourse
curl -s http://127.0.0.1/health
./scripts/bench_lan.sh
```

## Provider 与线上契约

配置 `provider: mock`（默认）可在纯本地验证链路；接入线上时改为：

```yaml
provider: http
http:
  base_url: https://<yongboWorkflow-host>
  token: <ASSET_SYNC_API_TOKEN>
  timeout_sec: 30
sync:
  kinds: [finalized]
  ticket_batch_size: 50
  verify_interval_sec: 86400
```

HTTP provider 只消费 `yongboWorkflow` 的 finalized 权威契约：

- `GET /v1/integration/asset-sync/finalized/manifest`
- `POST /v1/integration/asset-sync/finalized/download-tickets`
- 鉴权头：`X-Asset-Sync-Token`
- Manifest 使用弱 ETag；客户端保存 ETag，并在后续请求发送 `If-None-Match`。
- Ticket 每批最多 50 个 `task_asset_id`，只有 `ready` 才会下载。

旧的 `/sync/list?cursor=` 和 `/sync/download?storage_key=` 不再受支持。完整
Manifest 会原子更新 catalog；退出当前 finalized 的对象/item 只标记 tombstone，
不会自动删除本地文件。下载使用 `.part` 临时文件，完成大小与可用校验后再
atomic rename。`missing`、`size_mismatch`、`not_current` 或 `error` 均不会让
整体同步误报 ready；`retryable=true` 的错误会在下一轮自动重试。即使
Manifest 返回 304，也会按 `verify_interval_sec` 定期为全部当前对象重新申请
ticket，以发现同 key 覆盖或 OSS 删除。

## 统一素材库

对检索和订单打包来说，系统只有一个素材库。`finalized`、`library`、`archive`
只是内部存储与同步策略，不是面向用户的库。检索只返回本地可用素材，不暴露
内部来源和物理路径；订单打包按“当前版本优先、其他本地可用素材兜底”自动选择，
结果包内附带 `素材选择说明.txt`。

内部物理路径保持分开，避免首次同步时搬迁或重复复制大量数据；SQLite catalog
提供统一逻辑视图，因此不要求文件必须位于同一目录。

### 内部存储

| kind | 路径 | 用途 |
|---|---|---|
| `finalized` | `$DATA/finalized` | 线上当前版本缓存（自动优先） |
| `library` | `/home/resourse` | 既有本地素材（自动兜底） |
| `archive` | `$DATA/archive` | P4 历史冷库预留目录；当前 manifest/ticket provider 尚未支持 |

## 本地测试

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e . pytest
ASSET_HUB_CONFIG=/dev/null pytest -q
```

测试包含真实本地 HTTP stub：覆盖机器 token、弱 ETag/304、manifest/ticket
envelope、签名 URL 下载、元数据持久化、retryable 重试和快照退出 tombstone。
