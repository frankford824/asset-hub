from __future__ import annotations

import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

from asset_hub.config import Settings, ensure_data_dirs, get_settings

SKU_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_.]{2,}")


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS assets (
  asset_id TEXT PRIMARY KEY,
  task_asset_id INTEGER,
  kind TEXT NOT NULL,                 -- finalized | library | archive
  storage_key TEXT NOT NULL DEFAULT '',
  file_name TEXT NOT NULL DEFAULT '',
  original_filename TEXT NOT NULL DEFAULT '',
  file_size INTEGER NOT NULL DEFAULT 0,
  etag TEXT NOT NULL DEFAULT '',
  crc64_ecma TEXT NOT NULL DEFAULT '',
  whole_hash TEXT NOT NULL DEFAULT '',
  format TEXT NOT NULL DEFAULT '',
  mime_type TEXT NOT NULL DEFAULT '',
  sku_code TEXT NOT NULL DEFAULT '',
  sku_name TEXT NOT NULL DEFAULT '',
  local_path TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'ready', -- ready|missing|failed|skipped|tombstone
  updated_at REAL NOT NULL DEFAULT 0,
  deleted INTEGER NOT NULL DEFAULT 0,
  retryable INTEGER NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT '',
  manifest_id TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_assets_kind ON assets(kind);
CREATE INDEX IF NOT EXISTS idx_assets_storage ON assets(storage_key);
CREATE INDEX IF NOT EXISTS idx_assets_name ON assets(file_name);
CREATE INDEX IF NOT EXISTS idx_assets_sku ON assets(sku_code);
CREATE INDEX IF NOT EXISTS idx_assets_status ON assets(status);

CREATE TABLE IF NOT EXISTS finalized_items (
  revision_item_id INTEGER PRIMARY KEY,
  task_asset_id INTEGER NOT NULL,
  group_id INTEGER NOT NULL,
  revision_id INTEGER NOT NULL,
  revision_mode TEXT NOT NULL DEFAULT '',
  finalized_at REAL NOT NULL DEFAULT 0,
  task_id INTEGER NOT NULL DEFAULT 0,
  task_no TEXT NOT NULL DEFAULT '',
  scope_kind TEXT NOT NULL DEFAULT '',
  sku_code TEXT NOT NULL DEFAULT '',
  product_name TEXT NOT NULL DEFAULT '',
  sort_order INTEGER NOT NULL DEFAULT 0,
  item_name TEXT NOT NULL DEFAULT '',
  manifest_id TEXT NOT NULL DEFAULT '',
  deleted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_finalized_items_asset ON finalized_items(task_asset_id, deleted);
CREATE INDEX IF NOT EXISTS idx_finalized_items_group ON finalized_items(group_id, sort_order, revision_item_id);

CREATE TABLE IF NOT EXISTS sku_tokens (
  token TEXT NOT NULL,
  asset_id TEXT NOT NULL,
  PRIMARY KEY (token, asset_id)
);
CREATE INDEX IF NOT EXISTS idx_sku_token ON sku_tokens(token);

CREATE VIRTUAL TABLE IF NOT EXISTS assets_fts USING fts5(
  asset_id UNINDEXED,
  file_name,
  original_filename,
  storage_key,
  sku_code,
  sku_name
);

CREATE TABLE IF NOT EXISTS sync_state (
  kind TEXT PRIMARY KEY,
  cursor TEXT NOT NULL DEFAULT '',
  last_success_at REAL NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT '',
  ready INTEGER NOT NULL DEFAULT 0,
  stats_json TEXT NOT NULL DEFAULT '{}',
  etag TEXT NOT NULL DEFAULT '',
  manifest_id TEXT NOT NULL DEFAULT '',
  last_verified_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL,          -- queued|running|done|failed
  created_at REAL NOT NULL,
  started_at REAL,
  finished_at REAL,
  filename TEXT NOT NULL DEFAULT '',
  super_dir_name TEXT NOT NULL DEFAULT '',
  progress_json TEXT NOT NULL DEFAULT '{}',
  error TEXT NOT NULL DEFAULT '',
  archive_path TEXT NOT NULL DEFAULT '',
  meta_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created_at);
"""


def normalize_sku_token(value: str) -> str:
    return value.strip().upper()


def extract_sku_tokens(*texts: str) -> set[str]:
    out: set[str] = set()
    for text in texts:
        if not text:
            continue
        for m in SKU_TOKEN_RE.finditer(text):
            tok = normalize_sku_token(m.group(0))
            if len(tok) >= 3:
                out.add(tok)
    return out


@dataclass
class AssetRow:
    asset_id: str
    kind: str
    task_asset_id: int | None = None
    storage_key: str = ""
    file_name: str = ""
    original_filename: str = ""
    file_size: int = 0
    etag: str = ""
    crc64_ecma: str = ""
    whole_hash: str = ""
    format: str = ""
    mime_type: str = ""
    sku_code: str = ""
    sku_name: str = ""
    local_path: str = ""
    status: str = "ready"
    updated_at: float = 0
    deleted: int = 0
    retryable: int = 0
    last_error: str = ""
    manifest_id: str = ""


class Catalog:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        ensure_data_dirs(self.settings)
        self.db_path = self.settings.db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

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

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate_schema(conn)

    @staticmethod
    def _migrate_schema(conn: sqlite3.Connection) -> None:
        """Additive migration for catalogs created by the pre-manifest prototype."""
        asset_columns = {
            "task_asset_id": "INTEGER",
            "crc64_ecma": "TEXT NOT NULL DEFAULT ''",
            "format": "TEXT NOT NULL DEFAULT ''",
            "mime_type": "TEXT NOT NULL DEFAULT ''",
            "retryable": "INTEGER NOT NULL DEFAULT 0",
            "last_error": "TEXT NOT NULL DEFAULT ''",
            "manifest_id": "TEXT NOT NULL DEFAULT ''",
        }
        sync_columns = {
            "etag": "TEXT NOT NULL DEFAULT ''",
            "manifest_id": "TEXT NOT NULL DEFAULT ''",
            "last_verified_at": "REAL NOT NULL DEFAULT 0",
        }
        Catalog._add_columns(conn, "assets", asset_columns)
        Catalog._add_columns(conn, "sync_state", sync_columns)
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_assets_task_asset "
            "ON assets(task_asset_id) WHERE task_asset_id IS NOT NULL"
        )

    @staticmethod
    def _add_columns(
        conn: sqlite3.Connection, table: str, columns: dict[str, str]
    ) -> None:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, declaration in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

    def upsert_asset(self, asset: AssetRow) -> None:
        now = asset.updated_at or time.time()
        with self.connect() as conn:
            self._upsert_asset(conn, asset, now)

    @staticmethod
    def _upsert_asset(
        conn: sqlite3.Connection, asset: AssetRow, updated_at: float | None = None
    ) -> None:
        now = updated_at or asset.updated_at or time.time()
        conn.execute(
            """
            INSERT INTO assets (
              asset_id, task_asset_id, kind, storage_key, file_name, original_filename,
              file_size, etag, crc64_ecma, whole_hash, format, mime_type,
              sku_code, sku_name, local_path, status, updated_at, deleted,
              retryable, last_error, manifest_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(asset_id) DO UPDATE SET
              task_asset_id=excluded.task_asset_id,
              kind=excluded.kind,
              storage_key=excluded.storage_key,
              file_name=excluded.file_name,
              original_filename=excluded.original_filename,
              file_size=excluded.file_size,
              etag=excluded.etag,
              crc64_ecma=excluded.crc64_ecma,
              whole_hash=excluded.whole_hash,
              format=excluded.format,
              mime_type=excluded.mime_type,
              sku_code=excluded.sku_code,
              sku_name=excluded.sku_name,
              local_path=excluded.local_path,
              status=excluded.status,
              updated_at=excluded.updated_at,
              deleted=excluded.deleted,
              retryable=excluded.retryable,
              last_error=excluded.last_error,
              manifest_id=excluded.manifest_id
            """,
            (
                asset.asset_id,
                asset.task_asset_id,
                asset.kind,
                asset.storage_key,
                asset.file_name,
                asset.original_filename,
                asset.file_size,
                asset.etag,
                asset.crc64_ecma,
                asset.whole_hash,
                asset.format,
                asset.mime_type,
                asset.sku_code,
                asset.sku_name,
                asset.local_path,
                asset.status,
                now,
                asset.deleted,
                asset.retryable,
                asset.last_error,
                asset.manifest_id,
            ),
        )
        conn.execute("DELETE FROM sku_tokens WHERE asset_id=?", (asset.asset_id,))
        tokens = extract_sku_tokens(
            asset.sku_code, asset.sku_name, asset.file_name, asset.original_filename
        )
        conn.executemany(
            "INSERT OR IGNORE INTO sku_tokens(token, asset_id) VALUES (?,?)",
            [(token, asset.asset_id) for token in tokens],
        )
        conn.execute("DELETE FROM assets_fts WHERE asset_id=?", (asset.asset_id,))
        if not asset.deleted:
            conn.execute(
                """
                INSERT INTO assets_fts(asset_id, file_name, original_filename, storage_key, sku_code, sku_name)
                VALUES (?,?,?,?,?,?)
                """,
                (
                    asset.asset_id,
                    asset.file_name,
                    asset.original_filename,
                    asset.storage_key,
                    asset.sku_code,
                    asset.sku_name,
                ),
            )

    def get_asset(self, asset_id: str) -> AssetRow | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM assets WHERE asset_id=?", (asset_id,)
            ).fetchone()
            return self._row_to_asset(row) if row else None

    def mark_tombstone(self, asset_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE assets SET deleted=1, status='tombstone', updated_at=? WHERE asset_id=?",
                (time.time(), asset_id),
            )
            conn.execute("DELETE FROM assets_fts WHERE asset_id=?", (asset_id,))
            conn.execute("DELETE FROM sku_tokens WHERE asset_id=?", (asset_id,))

    def apply_finalized_manifest(
        self, items: Sequence[Any], manifest_id: str
    ) -> dict[str, int]:
        """Apply one complete snapshot atomically without deleting cached files."""
        objects: dict[int, Any] = {}
        for item in items:
            existing = objects.setdefault(int(item.task_asset_id), item)
            if (
                existing.storage_key != item.storage_key
                or existing.file_size != item.file_size
                or existing.whole_hash != item.whole_hash
                or existing.file_name != item.file_name
            ):
                raise ValueError(
                    f"conflicting manifest object task_asset_id={item.task_asset_id}"
                )

        with self.connect() as conn:
            before_current = int(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM assets WHERE kind='finalized' AND deleted=0"
                ).fetchone()["c"]
            )
            conn.execute(
                "CREATE TEMP TABLE IF NOT EXISTS snapshot_task_assets "
                "(task_asset_id INTEGER PRIMARY KEY)"
            )
            conn.execute("DELETE FROM snapshot_task_assets")
            conn.executemany(
                "INSERT INTO snapshot_task_assets(task_asset_id) VALUES (?)",
                [(task_asset_id,) for task_asset_id in objects],
            )
            conn.execute(
                "CREATE TEMP TABLE IF NOT EXISTS snapshot_revision_items "
                "(revision_item_id INTEGER PRIMARY KEY)"
            )
            conn.execute("DELETE FROM snapshot_revision_items")
            conn.executemany(
                "INSERT INTO snapshot_revision_items(revision_item_id) VALUES (?)",
                [(int(item.revision_item_id),) for item in items],
            )

            for task_asset_id, item in objects.items():
                previous = conn.execute(
                    "SELECT * FROM assets WHERE task_asset_id=?", (task_asset_id,)
                ).fetchone()
                unchanged = bool(
                    previous
                    and not previous["deleted"]
                    and previous["storage_key"] == item.storage_key
                    and int(previous["file_size"] or 0) == int(item.file_size)
                    and (previous["whole_hash"] or "") == (item.whole_hash or "")
                    and previous["file_name"] == item.file_name
                    and float(previous["updated_at"] or 0)
                    == float(item.asset_updated_at or 0)
                )
                asset = AssetRow(
                    asset_id=(previous["asset_id"] if previous else f"finalized:{task_asset_id}"),
                    task_asset_id=task_asset_id,
                    kind="finalized",
                    storage_key=item.storage_key,
                    file_name=item.file_name,
                    original_filename=item.original_filename or item.file_name,
                    file_size=int(item.file_size),
                    etag=(previous["etag"] or "") if unchanged else "",
                    crc64_ecma=(previous["crc64_ecma"] or "") if unchanged else "",
                    whole_hash=item.whole_hash or "",
                    format=item.format,
                    mime_type=item.mime_type,
                    sku_code=item.sku_code,
                    sku_name=item.product_name,
                    local_path=(previous["local_path"] or "") if unchanged else "",
                    status=(previous["status"] or "pending") if unchanged else "pending",
                    updated_at=float(item.asset_updated_at or time.time()),
                    deleted=0,
                    retryable=int(previous["retryable"] or 0) if unchanged else 1,
                    last_error=(previous["last_error"] or "") if unchanged else "",
                    manifest_id=manifest_id,
                )
                self._upsert_asset(conn, asset)

            exited_rows = conn.execute(
                """
                SELECT asset_id, task_asset_id FROM assets a
                WHERE kind='finalized' AND deleted=0
                  AND NOT EXISTS (
                    SELECT 1 FROM snapshot_task_assets s
                    WHERE s.task_asset_id=a.task_asset_id
                  )
                """
            ).fetchall()
            conn.execute(
                """
                UPDATE assets AS a
                   SET deleted=1, status='tombstone', retryable=0,
                       last_error='', manifest_id=?
                 WHERE kind='finalized' AND deleted=0
                   AND NOT EXISTS (
                     SELECT 1 FROM snapshot_task_assets s
                     WHERE s.task_asset_id=a.task_asset_id
                   )
                """,
                (manifest_id,),
            )
            for row in exited_rows:
                conn.execute("DELETE FROM assets_fts WHERE asset_id=?", (row["asset_id"],))
                conn.execute("DELETE FROM sku_tokens WHERE asset_id=?", (row["asset_id"],))

            for item in items:
                conn.execute(
                    """
                    INSERT INTO finalized_items (
                      revision_item_id, task_asset_id, group_id, revision_id,
                      revision_mode, finalized_at, task_id, task_no, scope_kind,
                      sku_code, product_name, sort_order, item_name, manifest_id, deleted
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)
                    ON CONFLICT(revision_item_id) DO UPDATE SET
                      task_asset_id=excluded.task_asset_id,
                      group_id=excluded.group_id,
                      revision_id=excluded.revision_id,
                      revision_mode=excluded.revision_mode,
                      finalized_at=excluded.finalized_at,
                      task_id=excluded.task_id,
                      task_no=excluded.task_no,
                      scope_kind=excluded.scope_kind,
                      sku_code=excluded.sku_code,
                      product_name=excluded.product_name,
                      sort_order=excluded.sort_order,
                      item_name=excluded.item_name,
                      manifest_id=excluded.manifest_id,
                      deleted=0
                    """,
                    (
                        int(item.revision_item_id),
                        int(item.task_asset_id),
                        int(item.group_id),
                        int(item.revision_id),
                        item.revision_mode,
                        float(item.finalized_at),
                        int(item.task_id),
                        item.task_no,
                        item.scope_kind,
                        item.sku_code,
                        item.product_name,
                        int(item.sort_order),
                        item.item_name,
                        manifest_id,
                    ),
                )
                asset_row = conn.execute(
                    "SELECT asset_id FROM assets WHERE task_asset_id=?",
                    (int(item.task_asset_id),),
                ).fetchone()
                asset_id = asset_row["asset_id"]
                extra_tokens = extract_sku_tokens(
                    item.sku_code, item.product_name, item.item_name
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO sku_tokens(token, asset_id) VALUES (?,?)",
                    [(token, asset_id) for token in extra_tokens],
                )
            conn.execute(
                """
                UPDATE finalized_items AS i
                   SET deleted=1, manifest_id=?
                 WHERE deleted=0
                   AND NOT EXISTS (
                     SELECT 1 FROM snapshot_revision_items s
                     WHERE s.revision_item_id=i.revision_item_id
                   )
                """,
                (manifest_id,),
            )
            after_current = len(objects)
            return {
                "objects": after_current,
                "items": len(items),
                "entered": max(0, after_current - (before_current - len(exited_rows))),
                "exited": len(exited_rows),
            }

    def list_finalized_assets(self) -> list[AssetRow]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM assets
                WHERE kind='finalized' AND deleted=0
                ORDER BY task_asset_id ASC
                """
            ).fetchall()
            return [self._row_to_asset(row) for row in rows]

    def list_finalized_items(self, *, include_deleted: bool = False) -> list[dict]:
        where = "" if include_deleted else "WHERE deleted=0"
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM finalized_items {where}
                ORDER BY group_id, sort_order, revision_item_id
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def get_asset_by_task_asset_id(self, task_asset_id: int) -> AssetRow | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM assets WHERE task_asset_id=?", (task_asset_id,)
            ).fetchone()
            return self._row_to_asset(row) if row else None

    def ticket_candidates(
        self, *, verify_all: bool = False, include_nonready: bool = False
    ) -> list[AssetRow]:
        rows = self.list_finalized_assets()
        candidates: list[AssetRow] = []
        for row in rows:
            local_valid = bool(
                row.local_path
                and Path(row.local_path).is_file()
                and Path(row.local_path).stat().st_size == row.file_size
            )
            if (
                verify_all
                or (include_nonready and row.status != "ready")
                or row.status == "pending"
                or row.retryable
                or (row.status == "ready" and not local_valid)
            ):
                candidates.append(row)
        return candidates

    def mark_task_asset_status(
        self,
        task_asset_id: int,
        status: str,
        *,
        local_path: str | None = None,
        etag: str | None = None,
        crc64_ecma: str | None = None,
        retryable: bool = False,
        error: str = "",
    ) -> None:
        fields = ["status=?", "retryable=?", "last_error=?"]
        values: list[Any] = [status, int(retryable), error]
        if local_path is not None:
            fields.append("local_path=?")
            values.append(local_path)
        if etag is not None:
            fields.append("etag=?")
            values.append(etag)
        if crc64_ecma is not None:
            fields.append("crc64_ecma=?")
            values.append(crc64_ecma)
        values.append(task_asset_id)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE assets SET {', '.join(fields)} WHERE task_asset_id=?", values
            )

    def mark_task_asset_tombstone(self, task_asset_id: int) -> None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT asset_id FROM assets WHERE task_asset_id=?", (task_asset_id,)
            ).fetchone()
            conn.execute(
                """
                UPDATE assets SET deleted=1, status='tombstone', retryable=0,
                                  last_error='not_current'
                WHERE task_asset_id=?
                """,
                (task_asset_id,),
            )
            conn.execute(
                "UPDATE finalized_items SET deleted=1 WHERE task_asset_id=?",
                (task_asset_id,),
            )
            if row:
                conn.execute("DELETE FROM assets_fts WHERE asset_id=?", (row["asset_id"],))
                conn.execute("DELETE FROM sku_tokens WHERE asset_id=?", (row["asset_id"],))

    def finalized_cache_complete(self, manifest_id: str) -> bool:
        state = self.get_sync_state("finalized")
        if not manifest_id or state.get("manifest_id") != manifest_id:
            return False
        for asset in self.list_finalized_assets():
            if asset.status != "ready" or not asset.local_path:
                return False
            path = Path(asset.local_path)
            if not path.is_file() or path.stat().st_size != asset.file_size:
                return False
        return True

    def search(
        self,
        query: str,
        *,
        kind: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[AssetRow], int]:
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        q = query.strip()
        priority_order = """
          CASE kind
            WHEN 'finalized' THEN 0
            WHEN 'library' THEN 1
            WHEN 'archive' THEN 2
            ELSE 3
          END,
          updated_at DESC,
          asset_id ASC
        """
        with self.connect() as conn:
            if not q:
                where = "deleted=0 AND status='ready'"
                args: list = []
                if kind:
                    where += " AND kind=?"
                    args.append(kind)
                total = int(
                    conn.execute(f"SELECT COUNT(*) AS c FROM assets WHERE {where}", args).fetchone()[
                        "c"
                    ]
                )
                rows = conn.execute(
                    f"SELECT * FROM assets WHERE {where} ORDER BY {priority_order} LIMIT ? OFFSET ?",
                    [*args, limit, offset],
                ).fetchall()
                return [self._row_to_asset(r) for r in rows], total

            token = normalize_sku_token(q)
            # exact SKU token first
            base = """
              FROM assets a
              JOIN sku_tokens t ON t.asset_id=a.asset_id
              WHERE t.token=? AND a.deleted=0 AND a.status='ready'
            """
            args = [token]
            if kind:
                base += " AND a.kind=?"
                args.append(kind)
            total = int(conn.execute(f"SELECT COUNT(*) AS c {base}", args).fetchone()["c"])
            if total:
                rows = conn.execute(
                    f"""SELECT a.* {base}
                    ORDER BY CASE a.kind
                      WHEN 'finalized' THEN 0
                      WHEN 'library' THEN 1
                      WHEN 'archive' THEN 2
                      ELSE 3 END,
                      a.updated_at DESC, a.asset_id ASC
                    LIMIT ? OFFSET ?""",
                    [*args, limit, offset],
                ).fetchall()
                return [self._row_to_asset(r) for r in rows], total

            # FTS fallback
            try:
                match = q.replace('"', " ")
                fts_term = " OR ".join(f"{part}*" for part in match.split() if part)
                if not fts_term:
                    fts_term = f"{match}*"
                base = """
                  FROM assets a
                  JOIN assets_fts f ON f.asset_id=a.asset_id
                  WHERE f MATCH ? AND a.deleted=0 AND a.status='ready'
                """
                args2: list = [fts_term]
                if kind:
                    base += " AND a.kind=?"
                    args2.append(kind)
                total = int(
                    conn.execute(f"SELECT COUNT(*) AS c {base}", args2).fetchone()["c"]
                )
                rows = conn.execute(
                    f"""SELECT a.* {base}
                    ORDER BY CASE a.kind
                      WHEN 'finalized' THEN 0
                      WHEN 'library' THEN 1
                      WHEN 'archive' THEN 2
                      ELSE 3 END,
                      a.updated_at DESC, a.asset_id ASC
                    LIMIT ? OFFSET ?""",
                    [*args2, limit, offset],
                ).fetchall()
                return [self._row_to_asset(r) for r in rows], total
            except sqlite3.OperationalError:
                like = f"%{q}%"
                where = """
                  deleted=0 AND status='ready' AND (
                    file_name LIKE ? OR original_filename LIKE ?
                    OR sku_code LIKE ? OR storage_key LIKE ?
                  )
                """
                args3: list = [like, like, like, like]
                if kind:
                    where += " AND kind=?"
                    args3.append(kind)
                total = int(
                    conn.execute(
                        f"SELECT COUNT(*) AS c FROM assets WHERE {where}", args3
                    ).fetchone()["c"]
                )
                rows = conn.execute(
                    f"SELECT * FROM assets WHERE {where} ORDER BY {priority_order} LIMIT ? OFFSET ?",
                    [*args3, limit, offset],
                ).fetchall()
                return [self._row_to_asset(r) for r in rows], total

    def count_ready(self, kind: str = "finalized") -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM assets WHERE kind=? AND deleted=0 AND status='ready'",
                (kind,),
            ).fetchone()
            return int(row["c"]) if row else 0

    def count_ready_all(self) -> int:
        """Count every locally usable asset in the unified catalog."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM assets WHERE deleted=0 AND status='ready'"
            ).fetchone()
            return int(row["c"]) if row else 0

    def order_current_assets(self, assets: Sequence[AssetRow]) -> list[AssetRow]:
        """Order current-version candidates by finalized metadata and remove duplicates."""
        by_id = {asset.asset_id: asset for asset in assets}
        if not by_id:
            return []
        placeholders = ",".join("?" for _ in by_id)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT a.asset_id
                FROM assets a
                LEFT JOIN (
                  SELECT task_asset_id,
                         MAX(finalized_at) AS finalized_at,
                         MIN(sort_order) AS sort_order
                  FROM finalized_items
                  WHERE deleted=0
                  GROUP BY task_asset_id
                ) i ON i.task_asset_id=a.task_asset_id
                WHERE a.asset_id IN ({placeholders})
                ORDER BY COALESCE(i.finalized_at, 0) DESC,
                         COALESCE(i.sort_order, 2147483647) ASC,
                         a.updated_at DESC,
                         a.asset_id ASC
                """,
                list(by_id),
            ).fetchall()
        return [by_id[row["asset_id"]] for row in rows]

    def set_sync_state(
        self,
        kind: str,
        *,
        cursor: str | None = None,
        etag: str | None = None,
        manifest_id: str | None = None,
        ready: bool | None = None,
        error: str | None = None,
        stats_json: str | None = None,
        success: bool = False,
        verified: bool = False,
    ) -> None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM sync_state WHERE kind=?", (kind,)
            ).fetchone()
            now = time.time()
            current = dict(row) if row else {}
            values = {
                "cursor": cursor if cursor is not None else current.get("cursor", ""),
                "last_success_at": now
                if success
                else current.get("last_success_at", 0),
                "last_error": error
                if error is not None
                else current.get("last_error", ""),
                "ready": int(ready)
                if ready is not None
                else int(current.get("ready", 0)),
                "stats_json": stats_json
                if stats_json is not None
                else current.get("stats_json", "{}"),
                "etag": etag if etag is not None else current.get("etag", ""),
                "manifest_id": manifest_id
                if manifest_id is not None
                else current.get("manifest_id", ""),
                "last_verified_at": now
                if verified
                else current.get("last_verified_at", 0),
            }
            conn.execute(
                """
                INSERT INTO sync_state (
                  kind, cursor, last_success_at, last_error, ready, stats_json,
                  etag, manifest_id, last_verified_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(kind) DO UPDATE SET
                  cursor=excluded.cursor,
                  last_success_at=excluded.last_success_at,
                  last_error=excluded.last_error,
                  ready=excluded.ready,
                  stats_json=excluded.stats_json,
                  etag=excluded.etag,
                  manifest_id=excluded.manifest_id,
                  last_verified_at=excluded.last_verified_at
                """,
                (
                    kind,
                    values["cursor"],
                    values["last_success_at"],
                    values["last_error"],
                    values["ready"],
                    values["stats_json"],
                    values["etag"],
                    values["manifest_id"],
                    values["last_verified_at"],
                ),
            )

    def get_sync_state(self, kind: str) -> dict:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM sync_state WHERE kind=?", (kind,)
            ).fetchone()
            if not row:
                return {
                    "kind": kind,
                    "cursor": "",
                    "ready": False,
                    "last_success_at": 0,
                    "last_error": "",
                    "stats_json": "{}",
                    "etag": "",
                    "manifest_id": "",
                    "last_verified_at": 0,
                }
            return dict(row)

    def is_finalized_ready(self) -> bool:
        st = self.get_sync_state("finalized")
        return bool(st.get("ready"))

    @staticmethod
    def _row_to_asset(row: sqlite3.Row) -> AssetRow:
        return AssetRow(
            asset_id=row["asset_id"],
            task_asset_id=int(row["task_asset_id"])
            if row["task_asset_id"] is not None
            else None,
            kind=row["kind"],
            storage_key=row["storage_key"] or "",
            file_name=row["file_name"] or "",
            original_filename=row["original_filename"] or "",
            file_size=int(row["file_size"] or 0),
            etag=row["etag"] or "",
            crc64_ecma=row["crc64_ecma"] or "",
            whole_hash=row["whole_hash"] or "",
            format=row["format"] or "",
            mime_type=row["mime_type"] or "",
            sku_code=row["sku_code"] or "",
            sku_name=row["sku_name"] or "",
            local_path=row["local_path"] or "",
            status=row["status"] or "ready",
            updated_at=float(row["updated_at"] or 0),
            deleted=int(row["deleted"] or 0),
            retryable=int(row["retryable"] or 0),
            last_error=row["last_error"] or "",
            manifest_id=row["manifest_id"] or "",
        )


def local_path_for_kind(
    settings: Settings, kind: str, asset_id: str, file_name: str
) -> Path:
    safe_name = Path(file_name).name or "file.bin"
    if kind == "archive":
        root = settings.archive_dir
    else:
        root = settings.finalized_dir
    return root / asset_id / safe_name


def local_path_for_finalized(settings: Settings, asset_id: str, file_name: str) -> Path:
    return local_path_for_kind(settings, "finalized", asset_id, file_name)
