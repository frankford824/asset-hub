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

### 目录工作台与文件名去重

资源库页面按 Linux 目录树方式展示统一逻辑目录，支持目录懒加载、面包屑、
缩略图、点击放大预览、右侧文件详情、Ctrl 多选、鼠标框选、单文件拖出下载和
多文件 ZIP 下载。文件或文件夹可以直接拖入页面，也可以通过上传按钮添加。

文件名唯一性按 Unicode NFC + 不区分大小写执行，并且作用于整个统一素材库，
不是仅限当前文件夹：

- 手动上传会先原子预占文件名；任一名称已存在时整批拒绝并返回重复文件清单，
  不会留下半批文件。
- finalized manifest 出现同名对象时复用已就绪的本地文件，并在 catalog 记录
  去重关系，不再申请 ticket 或重复下载。

这里采用“全库同名即同一资源”的严格语义。它满足当前去重要求，但也意味着
不同目录不能各自保存同名但内容不同的文件；若业务后续需要这种能力，应把
唯一键升级为“规范化目录 + 规范化文件名”或内容哈希，而不是放松当前检查。

### 可配置打包规则

首页展示 catalog 中的打包规则，支持新增、编辑、删除。新建任务时所有启用规则
默认勾选，用户可逐项取消；任务提交时会保存规则快照，后续编辑规则不会悄悄改变
已经排队的任务。当前内置规则已与 eve35 实际运行的
`C:\\eve-pack-server\\packaging_core.py`、`app.py` 和计划任务启动参数逐项核对：

| 内置规则 | eve35 行为 | asset-hub 实现 |
|---|---|---|
| 当前版本优先、库内兜底 | 首个匹配路径 | 当前 finalized 优先，再使用其他本地可用素材 |
| 按订单号分目录 | Excel 第 1 列分组 | 支持表头识别，并保留无表头时的第 1 列兼容 |
| 校验 SKU 格式 | `^[a-zA-Z]+\\d+$` | 同一正则 |
| 关键词二次筛选 | 第 5 列过滤候选路径 | 同步实现 |
| 按数量复制素材 | 第 3 列为复制数 | 同步实现 |
| SKU 顺序命名 | `SKU_1`、`SKU_2`… | 同步实现，但保留原扩展名；不沿用 eve35 强制写成 `.jpg` 的风险行为 |
| 地址文件、敏感标记 | 第 4 列；地址含 `*` 标 `_【敏感】` | 同步实现 |
| 缺失清单、未找全后缀 | `未找到编码.txt`、`_未找全` | 同步实现 |
| 素材选择说明 | eve35 无 | 保留 asset-hub 的可追溯增强 |
| 媒体快速 ZIP | 目录内 ZIP store，再生成外层 ZIP | 规则开启时同步实现 |

规则条目中的 `handler` 必须是服务支持的已注册处理器；CRUD 允许调整名称、说明、
参数和启用状态，但不会执行任意用户代码。

`asset-hub-index` 使用批量事务重建既有目录索引；index 与 finalized sync 通过
`/run/asset-hub/catalog.lock` 串行写 catalog。index timer 从一次索引完成后再计时
30 分钟，sync timer 从一次同步完成后再计时 5 分钟，避免任务耗时超过周期时
立即补跑并长期占用 SQLite 写锁或 CPU。

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
