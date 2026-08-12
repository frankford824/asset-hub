from __future__ import annotations

import os
import shutil
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from asset_hub import __version__
from asset_hub.catalog.db import Catalog
from asset_hub.config import ensure_data_dirs, get_settings
from asset_hub.jobs import JobStore


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_data_dirs()
    Catalog()
    yield


app = FastAPI(title="asset-hub", version=__version__, lifespan=lifespan)


def _x_accel(
    rel_under_data: str,
    filename: str | None = None,
    *,
    inline: bool = False,
) -> Response:
    headers = {"X-Accel-Redirect": f"/internal-files/{rel_under_data.lstrip('/')}"}
    if filename:
        mode = "inline" if inline else "attachment"
        headers["Content-Disposition"] = f'{mode}; filename="{filename}"'
    return Response(status_code=200, headers=headers)


def _serve_under_data(
    path: Path,
    data_root: Path,
    filename: str,
    *,
    inline: bool = False,
) -> Response:
    """Prefer nginx X-Accel; fall back to FileResponse for direct API access."""
    use_accel = os.environ.get("ASSET_HUB_X_ACCEL", "1") != "0"
    if use_accel:
        try:
            rel = path.resolve().relative_to(data_root.resolve()).as_posix()
            return _x_accel(rel, filename=filename, inline=inline)
        except ValueError:
            pass
    return FileResponse(
        path,
        filename=filename,
        content_disposition_type="inline" if inline else "attachment",
    )


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}


def _is_previewable(file_name: str) -> bool:
    return Path(file_name or "").suffix.lower() in IMAGE_EXTS


@app.get("/health")
def health() -> dict:
    s = get_settings()
    cat = Catalog(s)
    return {
        "ok": True,
        "version": __version__,
        "local_only": s.local_only,
        "provider": s.provider,
        "data_root": str(s.data_root),
        "finalized_ready": cat.is_finalized_ready(),
        "finalized_count": cat.count_ready("finalized"),
    }


@app.get("/api/v1/status")
def status() -> dict:
    s = get_settings()
    cat = Catalog(s)
    sync = cat.get_sync_state("finalized")
    return {
        "ready_for_pack": bool(sync.get("ready")),
        "local_only": s.local_only,
        "provider": s.provider,
        "workers": s.workers,
        "finalized_count": cat.count_ready("finalized"),
        "archive_count": cat.count_ready("archive"),
        "library_count": cat.count_ready("library"),
        "sync": sync,
        "paths": {
            "data_root": str(s.data_root),
            "finalized": str(s.finalized_dir),
            "archive": str(s.archive_dir),
            "jobs": str(s.jobs_dir),
            "library": str(s.library_root),
            "db": str(s.db_path),
        },
    }


@app.get("/api/v1/search")
def search(
    q: str = "",
    kind: str | None = Query(default=None),
    limit: int = 24,
    offset: int = 0,
) -> dict:
    cat = Catalog()
    rows, total = cat.search(q, kind=kind, limit=limit, offset=offset)
    return {
        "query": q,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(rows) < total,
        "results": [
            {
                "asset_id": r.asset_id,
                "kind": r.kind,
                "file_name": r.file_name,
                "file_size": r.file_size,
                "sku_code": r.sku_code,
                "sku_name": r.sku_name,
                "status": r.status,
                "local_path": r.local_path,
                "previewable": _is_previewable(r.file_name),
            }
            for r in rows
        ],
    }


@app.get("/api/v1/assets/{asset_id}/download")
def download_asset_path(
    asset_id: str,
    inline: bool = Query(default=False),
) -> Response:
    return _download_asset(asset_id, inline=inline)


@app.get("/api/v1/asset/download")
def download_asset_query(
    id: str = Query(..., min_length=1),
    inline: bool = Query(default=False),
) -> Response:
    return _download_asset(id, inline=inline)


def _download_asset(asset_id: str, *, inline: bool = False) -> Response:
    s = get_settings()
    cat = Catalog(s)
    asset = cat.get_asset(asset_id)
    if not asset or asset.deleted or asset.status != "ready" or not asset.local_path:
        raise HTTPException(404, "资产不可用或不在本地缓存")
    path = Path(asset.local_path)
    if not path.is_file():
        raise HTTPException(404, "本地文件缺失")
    # 默认下载为 attachment；显式 inline 或预览场景走 inline
    return _serve_under_data(
        path,
        s.data_root,
        asset.file_name or path.name,
        inline=inline,
    )


@app.get("/api/v1/assets/{asset_id}/preview")
def preview_asset_path(asset_id: str) -> Response:
    return _preview_asset(asset_id)


@app.get("/api/v1/asset/preview")
def preview_asset_query(id: str = Query(..., min_length=1)) -> Response:
    return _preview_asset(id)


def _preview_asset(asset_id: str) -> Response:
    """Always serve inline for lightbox / thumbnail."""
    s = get_settings()
    cat = Catalog(s)
    asset = cat.get_asset(asset_id)
    if not asset or asset.deleted or asset.status != "ready" or not asset.local_path:
        raise HTTPException(404, "资产不可用或不在本地缓存")
    path = Path(asset.local_path)
    if not path.is_file():
        raise HTTPException(404, "本地文件缺失")
    if not _is_previewable(asset.file_name or path.name):
        raise HTTPException(415, "该文件类型不支持预览")
    return _serve_under_data(
        path,
        s.data_root,
        asset.file_name or path.name,
        inline=True,
    )


@app.get("/api/v1/jobs")
def list_jobs(limit: int = 20) -> dict:
    store = JobStore()
    jobs = store.list(limit=limit)
    return {
        "jobs": [
            {
                "id": j.id,
                "status": j.status,
                "filename": j.filename,
                "created_at": j.created_at,
                "finished_at": j.finished_at,
                "progress": j.progress,
                "error": j.error,
                "has_download": bool(j.archive_path and Path(j.archive_path).is_file()),
            }
            for j in jobs
        ]
    }


@app.post("/api/v1/jobs")
async def create_job(
    file: UploadFile = File(...),
    super_dir_name: str = Form(""),
) -> dict:
    s = get_settings()
    cat = Catalog(s)
    if s.local_only and not cat.is_finalized_ready() and cat.count_ready("finalized") == 0:
        raise HTTPException(409, "终稿缓存未就绪（local_only），请先跑 sync")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".xlsx", ".xls"}:
        raise HTTPException(400, "仅支持 .xlsx / .xls")
    store = JobStore(s)
    job = store.create(filename=file.filename or f"input{suffix}", super_dir_name=super_dir_name)
    job_dir = store.job_dir(job.id)
    dest = job_dir / f"input{suffix}"
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    return {"job_id": job.id}


@app.get("/api/v1/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = JobStore().get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return {
        "id": job.id,
        "status": job.status,
        "filename": job.filename,
        "super_dir_name": job.super_dir_name,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "progress": job.progress,
        "error": job.error,
        "has_download": bool(job.archive_path and Path(job.archive_path).is_file()),
    }


@app.get("/api/v1/jobs/{job_id}/download")
def download_job(job_id: str) -> Response:
    s = get_settings()
    job = JobStore(s).get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    if job.status != "done" or not job.archive_path:
        raise HTTPException(409, "任务未完成")
    path = Path(job.archive_path)
    if not path.is_file():
        raise HTTPException(404, "结果文件不存在")
    name = f"{Path(job.filename or job_id).stem}_pack.zip"
    return _serve_under_data(path, s.data_root, name)


# optional static fallback when hitting API port directly (nginx normally serves web/)
_web = Path(__file__).resolve().parents[3] / "web" / "dist"
if _web.is_dir():
    app.mount("/", StaticFiles(directory=str(_web), html=True), name="web")


def run() -> None:
    import uvicorn

    s = get_settings()
    uvicorn.run(
        "asset_hub.api.main:app",
        host=s.api.host,
        port=s.api.port,
        workers=1,
        log_level="info",
    )


if __name__ == "__main__":
    run()
