from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Iterator, Sequence

from asset_hub.catalog.db import Catalog
from asset_hub.config import Settings, get_settings


@dataclass(frozen=True)
class PackRule:
    id: str
    rule_key: str
    name: str
    description: str
    handler: str
    enabled: bool = True
    sort_order: int = 0
    config: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# These are business rules cross-checked against eve35's live
# C:\eve-pack-server\packaging_core.py. Machine-specific search roots,
# Everything CLI and one-shot memo files intentionally do not belong here.
DEFAULT_RULES = (
    PackRule("rule-current", "current-first", "当前版本优先", "同名 SKU 有多个候选时优先选择当前终稿。", "prefer_current", sort_order=10),
    PackRule("rule-fallback", "library-fallback", "库内素材兜底", "当前终稿不可用时使用统一素材库中的可用文件。", "library_fallback", sort_order=20),
    PackRule("rule-order", "group-by-order", "按订单号分目录", "按 Excel 订单号生成独立目录；与 eve35 保持一致。", "group_by_order", sort_order=30),
    PackRule("rule-sku", "validate-sku", "校验 SKU 格式", "SKU 必须为英文字母开头并以数字结尾；不合规则记入缺失清单。", "validate_sku", sort_order=40),
    PackRule("rule-keyword", "keyword-filter", "关键词二次筛选", "Excel 提供关键词时，仅保留路径或文件信息中包含关键词的候选。", "keyword_filter", sort_order=50),
    PackRule("rule-quantity", "repeat-quantity", "按数量复制素材", "读取 Excel 数量列，按订购数量写入素材副本。", "repeat_quantity", sort_order=60),
    PackRule("rule-rename", "preserve-product-name", "保留商品名称与尺寸", "保留完整商品文件名；多图商品按描述目录归组；完全重复行合并，同编码但业务字段不同仍独立输出。", "rename_sku_sequence", sort_order=70),
    PackRule("rule-address", "write-address", "生成地址文件", "存在地址列时，在订单目录写入地址.txt。", "write_address", sort_order=80),
    PackRule("rule-sensitive", "mark-sensitive", "敏感订单标记", "地址包含 * 时，在订单目录名追加【敏感】。", "mark_sensitive", sort_order=90),
    PackRule("rule-missing", "missing-report", "生成缺失清单", "将格式错误、库内缺失和读取异常写入未找到编码.txt。", "missing_report", sort_order=100),
    PackRule("rule-incomplete", "mark-incomplete", "未找全目录标记", "订单存在缺失素材时，在目录名追加_未找全。", "mark_incomplete", sort_order=110),
    PackRule("rule-report", "selection-report", "生成选择说明", "在结果包中记录每行选择策略和缺失情况，便于复核。", "selection_report", sort_order=120),
    PackRule("rule-fast-zip", "fast-zip", "媒体文件快速打包", "图片、视频和已有压缩文件不重复压缩，降低等待时间。", "fast_zip", sort_order=130),
)

SUPPORTED_HANDLERS = {
    rule.handler: {"value": rule.handler, "label": rule.name}
    for rule in DEFAULT_RULES
}
SUPPORTED_HANDLERS["note"] = {"value": "note", "label": "说明条目（不改变文件）"}


class PackRuleStore:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.db_path = self.settings.db_path
        Catalog(self.settings)
        self._seed_once()
        self._migrate_preserved_names_once()
        self._migrate_exact_duplicate_policy_once()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=60)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _seed_once(self) -> None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM app_meta WHERE key='pack_rules_seed_v1'"
            ).fetchone()
            if row:
                return
            now = time.time()
            conn.executemany(
                """
                INSERT OR IGNORE INTO pack_rules(
                  id, rule_key, name, description, handler, enabled,
                  sort_order, config_json, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        rule.id,
                        rule.rule_key,
                        rule.name,
                        rule.description,
                        rule.handler,
                        int(rule.enabled),
                        rule.sort_order,
                        json.dumps(rule.config or {}, ensure_ascii=False),
                        now,
                        now,
                    )
                    for rule in DEFAULT_RULES
                ],
            )
            conn.execute(
                "INSERT OR REPLACE INTO app_meta(key, value) VALUES ('pack_rules_seed_v1', ?)",
                (str(now),),
            )

    def _migrate_preserved_names_once(self) -> None:
        marker = "pack_rules_preserve_names_v2"
        with self.connect() as conn:
            if conn.execute(
                "SELECT 1 FROM app_meta WHERE key=?", (marker,)
            ).fetchone():
                return
            now = time.time()
            conn.execute(
                """
                UPDATE pack_rules
                   SET rule_key='preserve-product-name',
                       name='保留商品名称与尺寸',
                       description='保留完整商品文件名；多图商品按描述目录归组，重复业务行按出现次数分别输出。',
                       updated_at=?
                 WHERE id='rule-rename'
                   AND handler='rename_sku_sequence'
                """,
                (now,),
            )
            conn.execute(
                "INSERT OR REPLACE INTO app_meta(key,value) VALUES(?,?)",
                (marker, str(now)),
            )

    def _migrate_exact_duplicate_policy_once(self) -> None:
        marker = "pack_rules_exact_duplicate_policy_v3"
        with self.connect() as conn:
            if conn.execute(
                "SELECT 1 FROM app_meta WHERE key=?", (marker,)
            ).fetchone():
                return
            now = time.time()
            conn.execute(
                """
                UPDATE pack_rules
                   SET description='保留完整商品文件名；多图商品按描述目录归组；完全重复行合并，同编码但业务字段不同仍独立输出。',
                       updated_at=?
                 WHERE id='rule-rename'
                   AND handler='rename_sku_sequence'
                """,
                (now,),
            )
            conn.execute(
                "INSERT OR REPLACE INTO app_meta(key,value) VALUES(?,?)",
                (marker, str(now)),
            )

    def list(self, *, enabled_only: bool = False) -> list[PackRule]:
        where = "WHERE enabled=1" if enabled_only else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM pack_rules {where} ORDER BY sort_order, created_at, id"
            ).fetchall()
        return [self._row(row) for row in rows]

    def get(self, rule_id: str) -> PackRule | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM pack_rules WHERE id=?", (rule_id,)).fetchone()
        return self._row(row) if row else None

    def create(
        self,
        *,
        name: str,
        description: str,
        handler: str,
        enabled: bool = True,
        sort_order: int = 1000,
        config: dict | None = None,
    ) -> PackRule:
        self._validate(name, handler)
        rule_id = f"rule-{uuid.uuid4().hex[:12]}"
        now = time.time()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO pack_rules(id, rule_key, name, description, handler,
                  enabled, sort_order, config_json, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    rule_id,
                    "custom",
                    name.strip(),
                    description.strip(),
                    handler,
                    int(enabled),
                    int(sort_order),
                    json.dumps(config or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return self.get(rule_id)  # type: ignore[return-value]

    def update(self, rule_id: str, **updates) -> PackRule | None:
        current = self.get(rule_id)
        if not current:
            return None
        name = str(updates.get("name", current.name)).strip()
        handler = str(updates.get("handler", current.handler)).strip()
        self._validate(name, handler)
        description = str(updates.get("description", current.description)).strip()
        enabled = bool(updates.get("enabled", current.enabled))
        sort_order = int(updates.get("sort_order", current.sort_order))
        config = updates.get("config", current.config or {})
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE pack_rules SET name=?, description=?, handler=?, enabled=?,
                  sort_order=?, config_json=?, updated_at=? WHERE id=?
                """,
                (
                    name,
                    description,
                    handler,
                    int(enabled),
                    sort_order,
                    json.dumps(config or {}, ensure_ascii=False),
                    time.time(),
                    rule_id,
                ),
            )
        return self.get(rule_id)

    def delete(self, rule_id: str) -> bool:
        with self.connect() as conn:
            conn.execute("DELETE FROM pack_rules WHERE id=?", (rule_id,))
            return conn.total_changes > 0

    def snapshots(self, rule_ids: Sequence[str] | None = None) -> list[dict]:
        rules = self.list(enabled_only=True)
        if rule_ids is None:
            return [rule.to_dict() for rule in rules]
        wanted = list(dict.fromkeys(rule_ids))
        by_id = {rule.id: rule for rule in rules}
        unknown = [rule_id for rule_id in wanted if rule_id not in by_id]
        if unknown:
            raise ValueError(f"规则不存在或已停用: {', '.join(unknown)}")
        return [by_id[rule_id].to_dict() for rule_id in wanted]

    @staticmethod
    def _validate(name: str, handler: str) -> None:
        if not name.strip():
            raise ValueError("规则名称不能为空")
        if handler not in SUPPORTED_HANDLERS:
            raise ValueError("不支持的规则行为")

    @staticmethod
    def _row(row: sqlite3.Row) -> PackRule:
        return PackRule(
            id=row["id"],
            rule_key=row["rule_key"] or "",
            name=row["name"],
            description=row["description"] or "",
            handler=row["handler"],
            enabled=bool(row["enabled"]),
            sort_order=int(row["sort_order"] or 0),
            config=json.loads(row["config_json"] or "{}"),
        )
