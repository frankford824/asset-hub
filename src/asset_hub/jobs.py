from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from asset_hub.config import Settings, get_settings


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
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.db_path = self.settings.db_path
        # schema created by Catalog; ensure jobs table exists via Catalog init side effect
        from asset_hub.catalog.db import Catalog

        Catalog(self.settings)

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

    def create(self, filename: str, super_dir_name: str = "", meta: dict | None = None) -> Job:
        job_id = uuid.uuid4().hex
        now = time.time()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs(id, status, created_at, filename, super_dir_name, progress_json, meta_json)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    job_id,
                    "queued",
                    now,
                    filename,
                    super_dir_name,
                    json.dumps({"percent": 0, "label": "queued"}, ensure_ascii=False),
                    json.dumps(meta or {}, ensure_ascii=False),
                ),
            )
        return self.get(job_id)  # type: ignore

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
        job_id: str | None = None
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE status='queued' ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            now = time.time()
            conn.execute(
                "UPDATE jobs SET status='running', started_at=?, progress_json=? WHERE id=? AND status='queued'",
                (
                    now,
                    json.dumps({"percent": 1, "label": "running"}, ensure_ascii=False),
                    row["id"],
                ),
            )
            if conn.total_changes == 0:
                return None
            job_id = row["id"]
        return self.get(job_id) if job_id else None

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
