from __future__ import annotations

import re
import sqlite3
import threading
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

from asset_hub.config import Settings, ensure_data_dirs, get_settings

SKU_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_.]{2,}")
SKU_QUERY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]*\d+$")


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

CREATE TABLE IF NOT EXISTS asset_name_claims (
  normalized_name TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL,
  claimed_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_asset_name_claim_asset ON asset_name_claims(asset_id);

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

CREATE TABLE IF NOT EXISTS pack_rules (
  id TEXT PRIMARY KEY,
  rule_key TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  handler TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  sort_order INTEGER NOT NULL DEFAULT 0,
  config_json TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pack_rules_sort ON pack_rules(sort_order, created_at);

CREATE TABLE IF NOT EXISTS app_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS download_selections (
  token TEXT PRIMARY KEY,
  asset_ids_json TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_download_selections_created ON download_selections(created_at);
"""


def normalize_sku_token(value: str) -> str:
    return value.strip().upper()


def normalize_file_name(value: str) -> str:
    """Global, Unicode-aware filename identity used by upload and OSS sync."""
    return unicodedata.normalize("NFC", Path(value or "").name.strip()).casefold()


def normalize_virtual_path(value: str) -> str:
    parts = []
    for raw in str(value or "").replace("\\", "/").split("/"):
        part = raw.strip()
        if not part or part in {".", ".."}:
            continue
        parts.append(part.replace("\x00", ""))
    return "/".join(parts)


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
    virtual_path: str = ""
    dedup_of_asset_id: str = ""


class Catalog:
    _schema_init_lock = threading.Lock()
    _schema_initialized_paths: set[str] = set()

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        ensure_data_dirs(self.settings)
        self.db_path = self.settings.db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Run migrations once and avoid writes for an already-current catalog."""
        key = str(self.db_path.resolve())
        if key in self._schema_initialized_paths:
            return
        with self._schema_init_lock:
            if key in self._schema_initialized_paths:
                return
            if not self._schema_is_current():
                self._init_schema()
            self._schema_initialized_paths.add(key)

    def _schema_is_current(self) -> bool:
        """Check schema read-only so API startup works during a long index write."""
        if not self.db_path.is_file():
            return False
        required_tables = {
            "assets",
            "asset_name_claims",
            "finalized_items",
            "sku_tokens",
            "assets_fts",
            "sync_state",
            "jobs",
            "pack_rules",
            "app_meta",
            "download_selections",
        }
        required_indexes = {
            "idx_assets_task_asset",
            "idx_assets_virtual_path",
            "idx_asset_name_claim_asset",
            "idx_pack_rules_sort",
        }
        required_asset_columns = {
            "task_asset_id",
            "crc64_ecma",
            "format",
            "mime_type",
            "retryable",
            "last_error",
            "manifest_id",
            "virtual_path",
            "dedup_of_asset_id",
        }
        required_sync_columns = {"etag", "manifest_id", "last_verified_at"}
        try:
            uri = f"{self.db_path.resolve().as_uri()}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=5) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if not required_tables.issubset(tables):
                    return False
                indexes = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'"
                    )
                }
                if not required_indexes.issubset(indexes):
                    return False
                asset_columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(assets)")
                }
                sync_columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(sync_state)")
                }
                if not required_asset_columns.issubset(asset_columns):
                    return False
                if not required_sync_columns.issubset(sync_columns):
                    return False
                return bool(
                    conn.execute(
                        "SELECT 1 FROM app_meta WHERE key='asset_name_claims_v1'"
                    ).fetchone()
                )
        except sqlite3.Error:
            return False

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
            "virtual_path": "TEXT NOT NULL DEFAULT ''",
            "dedup_of_asset_id": "TEXT NOT NULL DEFAULT ''",
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
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_assets_virtual_path ON assets(virtual_path)"
        )
        conn.execute(
            """
            UPDATE assets
               SET virtual_path=CASE
                 WHEN kind='library' AND storage_key<>'' THEN storage_key
                 ELSE file_name
               END
             WHERE virtual_path=''
            """
        )
        Catalog._backfill_name_claims(conn)

    @staticmethod
    def _backfill_name_claims(conn: sqlite3.Connection) -> None:
        seeded = conn.execute(
            "SELECT value FROM app_meta WHERE key='asset_name_claims_v1'"
        ).fetchone()
        if seeded:
            return
        rows = conn.execute(
            """
            SELECT asset_id, file_name, local_path FROM assets
             WHERE deleted=0 AND status='ready' AND file_name<>''
             ORDER BY CASE kind WHEN 'finalized' THEN 0 WHEN 'library' THEN 1 ELSE 2 END,
                      updated_at DESC, asset_id
            """
        ).fetchall()
        now = time.time()
        for row in rows:
            if not row["local_path"] or not Path(row["local_path"]).is_file():
                continue
            name_key = normalize_file_name(row["file_name"])
            if name_key:
                conn.execute(
                    "INSERT OR IGNORE INTO asset_name_claims(normalized_name, asset_id, claimed_at) VALUES (?,?,?)",
                    (name_key, row["asset_id"], now),
                )
        conn.execute(
            "INSERT OR REPLACE INTO app_meta(key, value) VALUES ('asset_name_claims_v1', ?)",
            (str(now),),
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

    def upsert_assets(self, assets: Sequence[AssetRow]) -> int:
        """Upsert a bounded batch in one transaction to avoid per-file commits."""
        rows = list(assets)
        if not rows:
            return 0
        with self.connect() as conn:
            for asset in rows:
                self._upsert_asset(conn, asset, asset.updated_at or time.time())
        return len(rows)

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
              retryable, last_error, manifest_id, virtual_path, dedup_of_asset_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
              manifest_id=excluded.manifest_id,
              virtual_path=excluded.virtual_path,
              dedup_of_asset_id=excluded.dedup_of_asset_id
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
                normalize_virtual_path(asset.virtual_path or asset.file_name),
                asset.dedup_of_asset_id,
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
        if not asset.deleted and asset.status == "ready" and asset.local_path:
            name_key = normalize_file_name(asset.file_name)
            if name_key:
                conn.execute(
                    "INSERT OR IGNORE INTO asset_name_claims(normalized_name, asset_id, claimed_at) VALUES (?,?,?)",
                    (name_key, asset.asset_id, now),
                )

    def get_asset(self, asset_id: str) -> AssetRow | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM assets WHERE asset_id=?", (asset_id,)
            ).fetchone()
            return self._row_to_asset(row) if row else None

    def library_fingerprints(self) -> dict[str, dict[str, Any]]:
        """Return the minimal state needed to skip unchanged filesystem rows."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT asset_id,file_name,file_size,updated_at,local_path,
                       virtual_path,status,deleted
                  FROM assets WHERE kind='library'
                """
            ).fetchall()
        return {row["asset_id"]: dict(row) for row in rows}

    def tombstone_library_assets(
        self, asset_ids: Sequence[str], *, batch_size: int = 25
    ) -> int:
        ids = [asset_id for asset_id in asset_ids if asset_id]
        changed = 0
        for start in range(0, len(ids), max(1, batch_size)):
            with self.connect() as conn:
                for asset_id in ids[start : start + batch_size]:
                    row = conn.execute(
                        """
                        SELECT file_name FROM assets
                         WHERE asset_id=? AND kind='library' AND deleted=0
                        """,
                        (asset_id,),
                    ).fetchone()
                    if not row:
                        continue
                    conn.execute(
                        """
                        UPDATE assets SET deleted=1,status='tombstone',updated_at=?
                         WHERE asset_id=?
                        """,
                        (time.time(), asset_id),
                    )
                    conn.execute("DELETE FROM assets_fts WHERE asset_id=?", (asset_id,))
                    conn.execute("DELETE FROM sku_tokens WHERE asset_id=?", (asset_id,))
                    conn.execute(
                        "DELETE FROM asset_name_claims WHERE asset_id=?", (asset_id,)
                    )
                    self._promote_name_claim(conn, row["file_name"])
                    changed += 1
        return changed

    def find_asset_by_name(
        self, file_name: str, *, exclude_asset_id: str = ""
    ) -> AssetRow | None:
        name_key = normalize_file_name(file_name)
        if not name_key:
            return None
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT a.* FROM asset_name_claims c
                JOIN assets a ON a.asset_id=c.asset_id
                WHERE c.normalized_name=? AND a.deleted=0 AND a.status='ready'
                  AND (?='' OR a.asset_id<>?)
                """,
                (name_key, exclude_asset_id, exclude_asset_id),
            ).fetchone()
            if not row:
                return None
            asset = self._row_to_asset(row)
            if asset.local_path and Path(asset.local_path).is_file():
                return asset
            return None

    def reserve_asset_names(self, items: Sequence[tuple[str, str]]) -> list[dict]:
        """Atomically reserve a whole upload batch or return every duplicate."""
        normalized: list[tuple[str, str, str]] = []
        seen: dict[str, str] = {}
        duplicates: list[dict] = []
        for file_name, asset_id in items:
            key = normalize_file_name(file_name)
            if not key:
                duplicates.append({"file_name": file_name, "reason": "invalid_name"})
                continue
            if key in seen:
                duplicates.append({"file_name": file_name, "reason": "duplicate_in_batch"})
            seen[key] = file_name
            normalized.append((key, file_name, asset_id))
        if duplicates:
            return duplicates
        now = time.time()
        with self.connect() as conn:
            # Serialize the read-then-reserve sequence so two concurrent upload
            # requests cannot both observe the same name as available.
            conn.execute("BEGIN IMMEDIATE")
            # Abandoned reservations are recoverable; fresh reservations still
            # protect concurrent uploads before their asset rows are inserted.
            conn.execute(
                """
                DELETE FROM asset_name_claims
                 WHERE claimed_at<?
                   AND NOT EXISTS (SELECT 1 FROM assets a WHERE a.asset_id=asset_name_claims.asset_id)
                """,
                (now - 600,),
            )
            for key, file_name, _asset_id in normalized:
                row = conn.execute(
                    "SELECT asset_id FROM asset_name_claims WHERE normalized_name=?",
                    (key,),
                ).fetchone()
                if row:
                    duplicates.append(
                        {"file_name": file_name, "reason": "filename_exists", "asset_id": row["asset_id"]}
                    )
            if duplicates:
                return duplicates
            conn.executemany(
                "INSERT INTO asset_name_claims(normalized_name, asset_id, claimed_at) VALUES (?,?,?)",
                [(key, asset_id, now) for key, _file_name, asset_id in normalized],
            )
        return []

    def release_asset_name_claims(self, asset_ids: Sequence[str]) -> None:
        ids = [value for value in asset_ids if value]
        if not ids:
            return
        with self.connect() as conn:
            conn.executemany(
                "DELETE FROM asset_name_claims WHERE asset_id=?",
                [(asset_id,) for asset_id in ids],
            )

    def mark_tombstone(self, asset_id: str) -> None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT file_name FROM assets WHERE asset_id=?", (asset_id,)
            ).fetchone()
            conn.execute(
                "UPDATE assets SET deleted=1, status='tombstone', updated_at=? WHERE asset_id=?",
                (time.time(), asset_id),
            )
            conn.execute("DELETE FROM assets_fts WHERE asset_id=?", (asset_id,))
            conn.execute("DELETE FROM sku_tokens WHERE asset_id=?", (asset_id,))
            conn.execute("DELETE FROM asset_name_claims WHERE asset_id=?", (asset_id,))
            if row:
                self._promote_name_claim(conn, row["file_name"])

    @staticmethod
    def _promote_name_claim(conn: sqlite3.Connection, file_name: str) -> None:
        """Keep a filename canonical when its previous owning row exits."""
        name_key = normalize_file_name(file_name)
        if not name_key:
            return
        exists = conn.execute(
            "SELECT 1 FROM asset_name_claims WHERE normalized_name=?", (name_key,)
        ).fetchone()
        if exists:
            return
        candidates = conn.execute(
            """
            SELECT asset_id, file_name, local_path FROM assets
             WHERE deleted=0 AND status='ready' AND file_name<>''
               AND file_name=? COLLATE NOCASE
             ORDER BY CASE kind WHEN 'finalized' THEN 0 WHEN 'library' THEN 1 ELSE 2 END,
                      updated_at DESC, asset_id
            """,
            (file_name,),
        ).fetchall()
        for candidate in candidates:
            if (
                normalize_file_name(candidate["file_name"]) == name_key
                and candidate["local_path"]
                and Path(candidate["local_path"]).is_file()
            ):
                # The Python-side normalization deliberately mirrors upload
                # semantics; SQLite NOCASE is ASCII-only and is insufficient.
                conn.execute(
                    "INSERT INTO asset_name_claims(normalized_name, asset_id, claimed_at) VALUES (?,?,?)",
                    (name_key, candidate["asset_id"], time.time()),
                )
                return

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
            existing_assets = {
                int(row["task_asset_id"]): row
                for row in conn.execute(
                    "SELECT * FROM assets WHERE kind='finalized' AND task_asset_id IS NOT NULL"
                ).fetchall()
            }
            existing_items = {
                int(row["revision_item_id"]): row
                for row in conn.execute("SELECT * FROM finalized_items").fetchall()
            }
            before_current = sum(not int(row["deleted"] or 0) for row in existing_assets.values())
            asset_ids: dict[int, str] = {}
            changed_objects = 0
            unchanged_objects = 0
            changed_items = 0
            unchanged_items = 0
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
                previous = existing_assets.get(task_asset_id)
                virtual_path = normalize_virtual_path(
                    f"{item.sku_code}/{item.file_name}" if item.sku_code else item.file_name
                )
                unchanged = bool(
                    previous
                    and not previous["deleted"]
                    and previous["storage_key"] == item.storage_key
                    and int(previous["file_size"] or 0) == int(item.file_size)
                    and (previous["whole_hash"] or "") == (item.whole_hash or "")
                    and previous["file_name"] == item.file_name
                    and previous["original_filename"]
                    == (item.original_filename or item.file_name)
                    and (previous["format"] or "") == (item.format or "")
                    and (previous["mime_type"] or "") == (item.mime_type or "")
                    and (previous["sku_code"] or "") == (item.sku_code or "")
                    and (previous["sku_name"] or "") == (item.product_name or "")
                    and (previous["virtual_path"] or "") == virtual_path
                    and float(previous["updated_at"] or 0)
                    == float(item.asset_updated_at or 0)
                )
                previous_asset_id = previous["asset_id"] if previous else f"finalized:{task_asset_id}"
                local_valid = bool(
                    previous
                    and previous["status"] == "ready"
                    and previous["local_path"]
                    and Path(previous["local_path"]).is_file()
                    and (
                        bool(previous["dedup_of_asset_id"])
                        or Path(previous["local_path"]).stat().st_size
                        == int(item.file_size)
                    )
                )
                if unchanged and local_valid:
                    asset_ids[task_asset_id] = previous_asset_id
                    unchanged_objects += 1
                    continue
                duplicate = conn.execute(
                    """
                    SELECT a.* FROM asset_name_claims c
                    JOIN assets a ON a.asset_id=c.asset_id
                    WHERE c.normalized_name=? AND a.asset_id<>?
                      AND a.deleted=0 AND a.status='ready' AND a.local_path<>''
                    """,
                    (normalize_file_name(item.file_name), previous_asset_id),
                ).fetchone()
                duplicate_valid = bool(
                    duplicate and Path(duplicate["local_path"]).is_file()
                )
                asset = AssetRow(
                    asset_id=previous_asset_id,
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
                    local_path=(
                        duplicate["local_path"]
                        if duplicate_valid
                        else ((previous["local_path"] or "") if unchanged else "")
                    ),
                    status=(
                        "ready"
                        if duplicate_valid
                        else ((previous["status"] or "pending") if unchanged else "pending")
                    ),
                    updated_at=float(item.asset_updated_at or time.time()),
                    deleted=0,
                    retryable=(
                        0
                        if duplicate_valid
                        else (int(previous["retryable"] or 0) if unchanged else 1)
                    ),
                    last_error=(previous["last_error"] or "") if unchanged else "",
                    manifest_id=manifest_id,
                    virtual_path=virtual_path,
                    dedup_of_asset_id=(
                        duplicate["asset_id"]
                        if duplicate_valid
                        else ((previous["dedup_of_asset_id"] or "") if unchanged else "")
                    ),
                )
                self._upsert_asset(conn, asset)
                asset_ids[task_asset_id] = previous_asset_id
                changed_objects += 1

            exited_rows = conn.execute(
                """
                SELECT asset_id, task_asset_id, file_name FROM assets a
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
                conn.execute("DELETE FROM asset_name_claims WHERE asset_id=?", (row["asset_id"],))
                self._promote_name_claim(conn, row["file_name"])

            for item in items:
                previous_item = existing_items.get(int(item.revision_item_id))
                item_values = (
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
                )
                previous_values = (
                    int(previous_item["task_asset_id"]),
                    int(previous_item["group_id"]),
                    int(previous_item["revision_id"]),
                    previous_item["revision_mode"],
                    float(previous_item["finalized_at"]),
                    int(previous_item["task_id"]),
                    previous_item["task_no"],
                    previous_item["scope_kind"],
                    previous_item["sku_code"],
                    previous_item["product_name"],
                    int(previous_item["sort_order"]),
                    previous_item["item_name"],
                ) if previous_item else None
                if previous_item and not int(previous_item["deleted"] or 0) and previous_values == item_values:
                    unchanged_items += 1
                    continue
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
                asset_id = asset_ids[int(item.task_asset_id)]
                extra_tokens = extract_sku_tokens(
                    item.sku_code, item.product_name, item.item_name
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO sku_tokens(token, asset_id) VALUES (?,?)",
                    [(token, asset_id) for token in extra_tokens],
                )
                changed_items += 1
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
                "changed_objects": changed_objects,
                "unchanged_objects": unchanged_objects,
                "changed_items": changed_items,
                "unchanged_items": unchanged_items,
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
                and (
                    bool(row.dedup_of_asset_id)
                    or Path(row.local_path).stat().st_size == row.file_size
                )
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
        dedup_of_asset_id: str | None = None,
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
        if dedup_of_asset_id is not None:
            fields.append("dedup_of_asset_id=?")
            values.append(dedup_of_asset_id)
        values.append(task_asset_id)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE assets SET {', '.join(fields)} WHERE task_asset_id=?", values
            )
            if status == "ready":
                row = conn.execute(
                    "SELECT asset_id, file_name, local_path FROM assets WHERE task_asset_id=?",
                    (task_asset_id,),
                ).fetchone()
                if row and row["local_path"] and Path(row["local_path"]).is_file():
                    name_key = normalize_file_name(row["file_name"])
                    if name_key:
                        conn.execute(
                            "INSERT OR IGNORE INTO asset_name_claims(normalized_name, asset_id, claimed_at) VALUES (?,?,?)",
                            (name_key, row["asset_id"], time.time()),
                        )

    def mark_task_asset_tombstone(self, task_asset_id: int) -> None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT asset_id, file_name FROM assets WHERE task_asset_id=?", (task_asset_id,)
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
                conn.execute("DELETE FROM asset_name_claims WHERE asset_id=?", (row["asset_id"],))
                self._promote_name_claim(conn, row["file_name"])

    def finalized_cache_complete(self, manifest_id: str) -> bool:
        state = self.get_sync_state("finalized")
        if not manifest_id or state.get("manifest_id") != manifest_id:
            return False
        for asset in self.list_finalized_assets():
            if asset.status != "ready" or not asset.local_path:
                return False
            path = Path(asset.local_path)
            if not path.is_file() or (
                not asset.dedup_of_asset_id and path.stat().st_size != asset.file_size
            ):
                return False
        return True

    def list_directory(
        self,
        virtual_path: str = "",
        *,
        query: str = "",
        limit: int = 200,
        offset: int = 0,
    ) -> tuple[list[dict], list[AssetRow], int]:
        """Return one level of the unified virtual tree without source labels."""
        base = normalize_virtual_path(virtual_path)
        prefix = f"{base}/" if base else ""
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT a.* FROM asset_name_claims c
                JOIN assets a ON a.asset_id=c.asset_id
                WHERE a.deleted=0 AND a.status='ready' AND a.virtual_path LIKE ?
                ORDER BY a.virtual_path COLLATE NOCASE, a.asset_id
                """,
                (prefix + "%",),
            ).fetchall()
        folders: dict[str, dict] = {}
        files: list[AssetRow] = []
        q = query.strip().casefold()
        for row in rows:
            full_path = normalize_virtual_path(row["virtual_path"])
            if not full_path.startswith(prefix):
                continue
            rel = full_path[len(prefix) :]
            if not rel:
                continue
            head, sep, _tail = rel.partition("/")
            if sep:
                entry = folders.setdefault(
                    head,
                    {"name": head, "path": f"{prefix}{head}", "file_count": 0},
                )
                entry["file_count"] += 1
                continue
            asset = self._row_to_asset(row)
            if q and q not in " ".join(
                [asset.file_name, asset.sku_code, asset.sku_name, asset.virtual_path]
            ).casefold():
                continue
            files.append(asset)
        files.sort(key=lambda item: (item.file_name.casefold(), item.asset_id))
        total = len(files)
        return (
            sorted(folders.values(), key=lambda item: item["name"].casefold()),
            files[offset : offset + limit],
            total,
        )

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
          CASE a.kind
            WHEN 'finalized' THEN 0
            WHEN 'library' THEN 1
            WHEN 'archive' THEN 2
            ELSE 3
          END,
          a.updated_at DESC,
          a.asset_id ASC
        """
        with self.connect() as conn:
            if not q:
                where = "a.deleted=0 AND a.status='ready'"
                args: list = []
                if kind:
                    where += " AND a.kind=?"
                    args.append(kind)
                total = int(
                    conn.execute(
                        f"SELECT COUNT(*) AS c FROM assets a JOIN asset_name_claims c ON c.asset_id=a.asset_id WHERE {where}",
                        args,
                    ).fetchone()[
                        "c"
                    ]
                )
                rows = conn.execute(
                    f"SELECT a.* FROM assets a JOIN asset_name_claims c ON c.asset_id=a.asset_id WHERE {where} ORDER BY {priority_order} LIMIT ? OFFSET ?",
                    [*args, limit, offset],
                ).fetchall()
                return [self._row_to_asset(r) for r in rows], total

            token = normalize_sku_token(q)
            # exact SKU token first
            base = """
              FROM assets a
              JOIN sku_tokens t ON t.asset_id=a.asset_id
              JOIN asset_name_claims c ON c.asset_id=a.asset_id
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

            # A syntactically complete SKU that is absent from the exact token
            # index cannot be rescued by prefix FTS. Returning immediately keeps
            # missing-code packaging O(1), especially for large catalogs.
            if SKU_QUERY_RE.fullmatch(q):
                return [], 0

            # FTS fallback
            try:
                match = q.replace('"', " ")
                fts_term = " OR ".join(f"{part}*" for part in match.split() if part)
                if not fts_term:
                    fts_term = f"{match}*"
                base = """
                  FROM assets a
                  JOIN asset_name_claims c ON c.asset_id=a.asset_id
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
                  a.deleted=0 AND a.status='ready' AND (
                    a.file_name LIKE ? OR a.original_filename LIKE ?
                    OR a.sku_code LIKE ? OR a.storage_key LIKE ?
                  )
                """
                args3: list = [like, like, like, like]
                if kind:
                    where += " AND a.kind=?"
                    args3.append(kind)
                total = int(
                    conn.execute(
                        f"SELECT COUNT(*) AS c FROM assets a JOIN asset_name_claims c ON c.asset_id=a.asset_id WHERE {where}", args3
                    ).fetchone()["c"]
                )
                rows = conn.execute(
                    f"SELECT a.* FROM assets a JOIN asset_name_claims c ON c.asset_id=a.asset_id WHERE {where} ORDER BY {priority_order} LIMIT ? OFFSET ?",
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
                """
                SELECT COUNT(*) AS c FROM asset_name_claims n
                JOIN assets a ON a.asset_id=n.asset_id
                WHERE a.deleted=0 AND a.status='ready'
                """
            ).fetchone()
            return int(row["c"]) if row else 0

    def create_download_selection(self, token: str, asset_ids_json: str) -> None:
        now = time.time()
        with self.connect() as conn:
            conn.execute("DELETE FROM download_selections WHERE created_at<?", (now - 900,))
            conn.execute(
                "INSERT INTO download_selections(token, asset_ids_json, created_at) VALUES (?,?,?)",
                (token, asset_ids_json, now),
            )

    def get_download_selection(self, token: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT asset_ids_json, created_at FROM download_selections WHERE token=?",
                (token,),
            ).fetchone()
            if not row or float(row["created_at"] or 0) < time.time() - 900:
                return None
            return str(row["asset_ids_json"])

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
            virtual_path=row["virtual_path"] or "",
            dedup_of_asset_id=row["dedup_of_asset_id"] or "",
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
