from __future__ import annotations

import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from asset_hub.config import Settings, ensure_data_dirs, get_settings

SKU_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_.]{2,}")


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS assets (
  asset_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,                 -- finalized | library | archive
  storage_key TEXT NOT NULL DEFAULT '',
  file_name TEXT NOT NULL DEFAULT '',
  original_filename TEXT NOT NULL DEFAULT '',
  file_size INTEGER NOT NULL DEFAULT 0,
  etag TEXT NOT NULL DEFAULT '',
  whole_hash TEXT NOT NULL DEFAULT '',
  sku_code TEXT NOT NULL DEFAULT '',
  sku_name TEXT NOT NULL DEFAULT '',
  local_path TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'ready', -- ready|missing|failed|skipped|tombstone
  updated_at REAL NOT NULL DEFAULT 0,
  deleted INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_assets_kind ON assets(kind);
CREATE INDEX IF NOT EXISTS idx_assets_storage ON assets(storage_key);
CREATE INDEX IF NOT EXISTS idx_assets_name ON assets(file_name);
CREATE INDEX IF NOT EXISTS idx_assets_sku ON assets(sku_code);
CREATE INDEX IF NOT EXISTS idx_assets_status ON assets(status);

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
  stats_json TEXT NOT NULL DEFAULT '{}'
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
    storage_key: str = ""
    file_name: str = ""
    original_filename: str = ""
    file_size: int = 0
    etag: str = ""
    whole_hash: str = ""
    sku_code: str = ""
    sku_name: str = ""
    local_path: str = ""
    status: str = "ready"
    updated_at: float = 0
    deleted: int = 0


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

    def upsert_asset(self, asset: AssetRow) -> None:
        now = asset.updated_at or time.time()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO assets (
                  asset_id, kind, storage_key, file_name, original_filename,
                  file_size, etag, whole_hash, sku_code, sku_name,
                  local_path, status, updated_at, deleted
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(asset_id) DO UPDATE SET
                  kind=excluded.kind,
                  storage_key=excluded.storage_key,
                  file_name=excluded.file_name,
                  original_filename=excluded.original_filename,
                  file_size=excluded.file_size,
                  etag=excluded.etag,
                  whole_hash=excluded.whole_hash,
                  sku_code=excluded.sku_code,
                  sku_name=excluded.sku_name,
                  local_path=excluded.local_path,
                  status=excluded.status,
                  updated_at=excluded.updated_at,
                  deleted=excluded.deleted
                """,
                (
                    asset.asset_id,
                    asset.kind,
                    asset.storage_key,
                    asset.file_name,
                    asset.original_filename,
                    asset.file_size,
                    asset.etag,
                    asset.whole_hash,
                    asset.sku_code,
                    asset.sku_name,
                    asset.local_path,
                    asset.status,
                    now,
                    asset.deleted,
                ),
            )
            conn.execute("DELETE FROM sku_tokens WHERE asset_id=?", (asset.asset_id,))
            tokens = extract_sku_tokens(
                asset.sku_code, asset.sku_name, asset.file_name, asset.original_filename
            )
            conn.executemany(
                "INSERT OR IGNORE INTO sku_tokens(token, asset_id) VALUES (?,?)",
                [(t, asset.asset_id) for t in tokens],
            )
            # FTS refresh for this row
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
        with self.connect() as conn:
            if not q:
                where = "deleted=0"
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
                    f"SELECT * FROM assets WHERE {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                    [*args, limit, offset],
                ).fetchall()
                return [self._row_to_asset(r) for r in rows], total

            token = normalize_sku_token(q)
            # exact SKU token first
            base = """
              FROM assets a
              JOIN sku_tokens t ON t.asset_id=a.asset_id
              WHERE t.token=? AND a.deleted=0
            """
            args = [token]
            if kind:
                base += " AND a.kind=?"
                args.append(kind)
            total = int(conn.execute(f"SELECT COUNT(*) AS c {base}", args).fetchone()["c"])
            if total:
                rows = conn.execute(
                    f"SELECT a.* {base} ORDER BY a.updated_at DESC LIMIT ? OFFSET ?",
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
                  WHERE f MATCH ? AND a.deleted=0
                """
                args2: list = [fts_term]
                if kind:
                    base += " AND a.kind=?"
                    args2.append(kind)
                total = int(
                    conn.execute(f"SELECT COUNT(*) AS c {base}", args2).fetchone()["c"]
                )
                rows = conn.execute(
                    f"SELECT a.* {base} LIMIT ? OFFSET ?",
                    [*args2, limit, offset],
                ).fetchall()
                return [self._row_to_asset(r) for r in rows], total
            except sqlite3.OperationalError:
                like = f"%{q}%"
                where = """
                  deleted=0 AND (
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
                    f"SELECT * FROM assets WHERE {where} LIMIT ? OFFSET ?",
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

    def set_sync_state(
        self,
        kind: str,
        *,
        cursor: str | None = None,
        ready: bool | None = None,
        error: str | None = None,
        stats_json: str | None = None,
        success: bool = False,
    ) -> None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT cursor, ready FROM sync_state WHERE kind=?", (kind,)
            ).fetchone()
            cur = cursor if cursor is not None else (row["cursor"] if row else "")
            rd = int(ready) if ready is not None else (int(row["ready"]) if row else 0)
            err = error if error is not None else ""
            stats = stats_json if stats_json is not None else "{}"
            last_ok = time.time() if success else (0 if not row else None)
            if row is None:
                conn.execute(
                    """
                    INSERT INTO sync_state(kind, cursor, last_success_at, last_error, ready, stats_json)
                    VALUES (?,?,?,?,?,?)
                    """,
                    (kind, cur, time.time() if success else 0, err, rd, stats),
                )
            else:
                if last_ok is None:
                    conn.execute(
                        """
                        UPDATE sync_state SET cursor=?, last_error=?, ready=?, stats_json=?
                        WHERE kind=?
                        """,
                        (cur, err, rd, stats, kind),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE sync_state SET cursor=?, last_success_at=?, last_error=?, ready=?, stats_json=?
                        WHERE kind=?
                        """,
                        (cur, last_ok, err, rd, stats, kind),
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
                }
            return dict(row)

    def is_finalized_ready(self) -> bool:
        st = self.get_sync_state("finalized")
        return bool(st.get("ready"))

    @staticmethod
    def _row_to_asset(row: sqlite3.Row) -> AssetRow:
        return AssetRow(
            asset_id=row["asset_id"],
            kind=row["kind"],
            storage_key=row["storage_key"] or "",
            file_name=row["file_name"] or "",
            original_filename=row["original_filename"] or "",
            file_size=int(row["file_size"] or 0),
            etag=row["etag"] or "",
            whole_hash=row["whole_hash"] or "",
            sku_code=row["sku_code"] or "",
            sku_name=row["sku_name"] or "",
            local_path=row["local_path"] or "",
            status=row["status"] or "ready",
            updated_at=float(row["updated_at"] or 0),
            deleted=int(row["deleted"] or 0),
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
