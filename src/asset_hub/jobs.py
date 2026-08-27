from __future__ import annotations

import json
import re
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from asset_hub.config import Settings, get_settings


JOBS_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA temp_store=MEMORY;

CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
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
CREATE TABLE IF NOT EXISTS app_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT ''
);
"""


@dataclass
class Job:
    id: str
    status: str
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    filename: str = ""
    super_dir_name: str = ""
    progress: dict | None = None
    error: str = ""
    archive_path: str = ""
    meta: dict | None = None


class JobStore:
    _init_lock = threading.Lock()
    _initialized_paths: set[str] = set()

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.db_path = self.settings.jobs_db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        key = str(self.db_path.resolve())
        if key in self._initialized_paths:
            return
        with self._init_lock:
            if key in self._initialized_paths:
                return
            with sqlite3.connect(self.db_path, timeout=10) as conn:
                conn.executescript(JOBS_SCHEMA)
            self._migrate_legacy_jobs()
            self._initialized_paths.add(key)

    def _migrate_legacy_jobs(self) -> None:
        legacy = self.settings.db_path
        if not legacy.is_file() or legacy.resolve() == self.db_path.resolve():
            return
        with self.connect() as conn:
            if conn.execute(
                "SELECT 1 FROM app_meta WHERE key='legacy_jobs_migrated_v1'"
            ).fetchone():
                return
        try:
            uri = f"{legacy.resolve().as_uri()}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=10) as old:
                exists = old.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
                ).fetchone()
                rows = old.execute("SELECT * FROM jobs").fetchall() if exists else []
        except sqlite3.Error:
            rows = []
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO jobs(
                  id,status,created_at,started_at,finished_at,filename,
                  super_dir_name,progress_json,error,archive_path,meta_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                rows,
            )
            conn.execute(
                "INSERT OR REPLACE INTO app_meta(key,value) VALUES('legacy_jobs_migrated_v1',?)",
                (str(time.time()),),
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create(
        self,
        filename: str,
        super_dir_name: str = "",
        meta: dict | None = None,
        *,
        enqueue: bool = True,
    ) -> Job:
        job_id = uuid.uuid4().hex
        now = time.time()
        status = "queued" if enqueue else "uploading"
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs(id, status, created_at, filename, super_dir_name, progress_json, meta_json)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    job_id,
                    status,
                    now,
                    filename,
                    super_dir_name,
                    json.dumps(
                        {"percent": 0, "label": "queued" if enqueue else "uploading"},
                        ensure_ascii=False,
                    ),
                    json.dumps(meta or {}, ensure_ascii=False),
                ),
            )
        return self.get(job_id)  # type: ignore

    def enqueue(self, job_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE jobs SET status='queued', progress_json=?
                 WHERE id=? AND status='uploading'
                """,
                (json.dumps({"percent": 0, "label": "queued"}, ensure_ascii=False), job_id),
            )

    def get(self, job_id: str) -> Job | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return self._to_job(row) if row else None

    def list(self, limit: int = 20) -> list[Job]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
                (max(1, min(limit, 100)),),
            ).fetchall()
            return [self._to_job(r) for r in rows]

    def claim_next(self) -> Job | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                UPDATE jobs
                   SET status='running', started_at=?, progress_json=?
                 WHERE id=(
                   SELECT id FROM jobs WHERE status='queued'
                   ORDER BY created_at ASC LIMIT 1
                 )
                   AND status='queued'
                RETURNING *
                """,
                (
                    time.time(),
                    json.dumps({"percent": 1, "label": "running"}, ensure_ascii=False),
                ),
            ).fetchone()
            return self._to_job(row) if row else None

    def requeue_interrupted(self) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE jobs
                   SET status='queued', started_at=NULL, finished_at=NULL,
                       progress_json=?, error=''
                 WHERE status='running'
                """,
                (json.dumps({"percent": 0, "label": "requeued"}, ensure_ascii=False),),
            )
            return int(cursor.rowcount)

    def update(
        self,
        job_id: str,
        *,
        status: str | None = None,
        progress: dict | None = None,
        error: str | None = None,
        archive_path: str | None = None,
        finished: bool = False,
    ) -> None:
        fields = []
        args: list = []
        if status is not None:
            fields.append("status=?")
            args.append(status)
        if progress is not None:
            fields.append("progress_json=?")
            args.append(json.dumps(progress, ensure_ascii=False))
        if error is not None:
            fields.append("error=?")
            args.append(error)
        if archive_path is not None:
            fields.append("archive_path=?")
            args.append(archive_path)
        if finished:
            fields.append("finished_at=?")
            args.append(time.time())
        if not fields:
            return
        args.append(job_id)
        with self.connect() as conn:
            conn.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id=?", args)

    def job_dir(self, job_id: str) -> Path:
        d = self.settings.jobs_dir / job_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def prune_finished(
        self,
        retention_hours: int,
        *,
        keep_recent: int = 20,
        now: float | None = None,
    ) -> tuple[int, int]:
        """Remove expired terminal job files and rows, preserving recent jobs."""
        cutoff = (time.time() if now is None else now) - retention_hours * 3600
        with self.connect() as conn:
            terminal = conn.execute(
                """
                SELECT id, COALESCE(finished_at, created_at) AS terminal_at
                  FROM jobs
                 WHERE status IN ('done', 'failed')
                 ORDER BY created_at DESC
                """
            ).fetchall()
        candidates = [
            str(row["id"])
            for row in terminal[max(0, keep_recent) :]
            if float(row["terminal_at"] or 0) < cutoff
        ]

        jobs_root = self.settings.jobs_dir.resolve()
        deleted_ids: list[str] = []
        deleted_bytes = 0
        for job_id in candidates:
            if not re.fullmatch(r"[0-9a-f]{32}", job_id):
                continue
            job_dir = self.settings.jobs_dir / job_id
            if job_dir.is_symlink():
                continue
            resolved = job_dir.resolve()
            if resolved.parent != jobs_root:
                continue
            if resolved.is_dir():
                deleted_bytes += sum(
                    entry.stat().st_size
                    for entry in resolved.rglob("*")
                    if entry.is_file() and not entry.is_symlink()
                )
                shutil.rmtree(resolved)
            deleted_ids.append(job_id)

        if deleted_ids:
            with self.connect() as conn:
                conn.executemany("DELETE FROM jobs WHERE id=?", ((job_id,) for job_id in deleted_ids))
        return len(deleted_ids), deleted_bytes

    @staticmethod
    def _to_job(row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            status=row["status"],
            created_at=float(row["created_at"] or 0),
            started_at=float(row["started_at"]) if row["started_at"] is not None else None,
            finished_at=float(row["finished_at"]) if row["finished_at"] is not None else None,
            filename=row["filename"] or "",
            super_dir_name=row["super_dir_name"] or "",
            progress=json.loads(row["progress_json"] or "{}"),
            error=row["error"] or "",
            archive_path=row["archive_path"] or "",
            meta=json.loads(row["meta_json"] or "{}"),
        )
