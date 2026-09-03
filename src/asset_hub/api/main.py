from __future__ import annotations

import os
import json
import shutil
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from asset_hub import __version__
from asset_hub.catalog.db import AssetRow, Catalog, normalize_virtual_path
from asset_hub.config import ensure_data_dirs, get_settings, library_mount_available
from asset_hub.jobs import JobStore
from asset_hub.pack.rules import PackRuleStore, SUPPORTED_HANDLERS
from asset_hub.pack.excel import deduplicate_rows, ensure_sku_in_filename, read_excel_rows
from asset_hub.pack.ziputil import zip_paths


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_data_dirs()
    Catalog()
    JobStore()
    yield


app = FastAPI(title="asset-hub", version=__version__, lifespan=lifespan)

_status_cache_lock = threading.Lock()
_status_cache_at = 0.0
_status_cache: dict = {}
_jobs_cache_lock = threading.Lock()
_jobs_cache_at = 0.0
_jobs_cache: dict = {"jobs": []}
_download_cache_lock = threading.Lock()
_download_cache: dict[str, tuple[str, str]] = {}


def _status_snapshot(max_age: float = 1.0) -> dict:
    global _status_cache_at, _status_cache
    now = time.monotonic()
    if now - _status_cache_at <= max_age and _status_cache:
        return _status_cache
    acquired = _status_cache_lock.acquire(blocking=not bool(_status_cache))
    if not acquired:
        return _status_cache
    try:
        now = time.monotonic()
        if now - _status_cache_at <= max_age and _status_cache:
            return _status_cache
        s = get_settings()
        cat = Catalog(s)
        sync = cat.get_sync_state("finalized")
        external_sync = cat.get_sync_state("external")
        external_follow = cat.get_sync_state("external_follow")
        asset_count = cat.count_ready_all()
        library_mounted = library_mount_available(s)
        required_sync_states = [sync]
        if "external" in s.sync.kinds:
            required_sync_states.append(external_sync)
        _status_cache = {
            "ready_for_pack": asset_count > 0 and library_mounted,
            "sync_complete": all(bool(item.get("ready")) for item in required_sync_states),
            "asset_count": asset_count,
            "local_only": s.local_only,
            "provider": s.provider,
            "workers": s.workers,
            "pack_workers": s.pack_workers,
            "job_retention_hours": s.job_retention_hours,
            "api_workers": s.api.workers,
            "download_transport": "x_accel" if s.api.x_accel else "stream",
            "finalized_count": cat.count_ready("finalized"),
            "finalized_ready": cat.is_finalized_ready(),
            "external_count": cat.count_ready("external"),
            "external_ready": bool(external_sync.get("ready")),
            "archive_count": cat.count_ready("archive"),
            "library_count": cat.count_ready("library"),
            "library_mount_required": s.library_mount_required,
            "library_mounted": library_mounted,
            "sync": sync,
            "external_sync": external_sync,
            "external_follow": external_follow,
            "paths": {
                "data_root": str(s.data_root),
                "finalized": str(s.finalized_dir),
                "archive": str(s.archive_dir),
                "jobs": str(s.jobs_dir),
                "library": str(s.library_root),
                "db": str(s.db_path),
            },
        }
        _status_cache_at = now
        return _status_cache
    finally:
        _status_cache_lock.release()


def _x_accel(
    rel_under_data: str,
    filename: str | None = None,
    *,
    inline: bool = False,
) -> Response:
    internal_uri = quote(rel_under_data.lstrip("/"), safe="/")
    headers = {"X-Accel-Redirect": f"/internal-files/{internal_uri}"}
    if filename:
        mode = "inline" if inline else "attachment"
        quoted = quote(filename, safe="")
        headers["Content-Disposition"] = (
            f"{mode}; filename*=utf-8''{quoted}"
            if quoted != filename
            else f'{mode}; filename="{filename}"'
        )
    return Response(status_code=200, headers=headers)


def _serve_under_data(
    path: Path,
    data_root: Path,
    filename: str,
    *,
    inline: bool = False,
    use_accel: bool | None = None,
) -> Response:
    """Prefer nginx X-Accel; fall back to FileResponse for direct API access."""
    if use_accel is None:
        use_accel = os.environ.get("ASSET_HUB_X_ACCEL", "1") != "0"
    if use_accel:
        try:
            rel = path.resolve().relative_to(data_root.resolve()).as_posix()
        except ValueError:
            pass
        else:
            return _x_accel(rel, filename=filename, inline=inline)
    return FileResponse(
        path,
        filename=filename,
        content_disposition_type="inline" if inline else "attachment",
    )


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}


class AssetIdsRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=200)


class PackRulePayload(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    handler: str
    enabled: bool = True
    sort_order: int = 1000
    config: dict = Field(default_factory=dict)


def _is_previewable(file_name: str) -> bool:
    return Path(file_name or "").suffix.lower() in IMAGE_EXTS


def _asset_json(asset: AssetRow) -> dict:
    return {
        "asset_id": asset.asset_id,
        "file_name": asset.file_name,
        "file_size": asset.file_size,
        "sku_code": asset.sku_code,
        "sku_name": asset.sku_name,
        "virtual_path": asset.virtual_path,
        "updated_at": asset.updated_at,
        "previewable": _is_previewable(asset.file_name),
        "deduplicated": bool(asset.dedup_of_asset_id),
    }


def _safe_library_target(root: Path, virtual_path: str) -> Path:
    clean = normalize_virtual_path(virtual_path)
    target = (root / clean).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError as exc:
        raise HTTPException(400, "目标目录无效") from exc
    return target


def _download_archive(asset_ids: list[str], background_tasks: BackgroundTasks) -> FileResponse:
    settings = get_settings()
    catalog = Catalog(settings)
    ids = list(dict.fromkeys(asset_ids))[:200]
    selected: list[tuple[AssetRow, Path]] = []
    for asset_id in ids:
        asset = catalog.get_asset(asset_id)
        if not asset or asset.deleted or asset.status != "ready" or not asset.local_path:
            continue
        source = Path(asset.local_path)
        if source.is_file():
            selected.append((asset, source))
    sku_codes = catalog.resolve_asset_sku_codes([asset for asset, _source in selected])
    pairs: list[tuple[Path, str]] = []
    used_names: dict[str, int] = {}
    for asset, source in selected:
        export_name = ensure_sku_in_filename(
            asset.file_name, sku_codes.get(asset.asset_id, "")
        )
        count = used_names.get(export_name, 0) + 1
        used_names[export_name] = count
        arcname = export_name
        if count > 1:
            export_path = Path(export_name)
            arcname = f"{export_path.stem}_{count}{export_path.suffix}"
        pairs.append((source, arcname))
    if not pairs:
        raise HTTPException(404, "所选素材均不可用")
    archive = settings.tmp_dir / f"素材下载-{uuid.uuid4().hex}.zip"
    zip_paths(pairs, archive)
    background_tasks.add_task(archive.unlink, missing_ok=True)
    return FileResponse(archive, filename="素材下载.zip", media_type="application/zip")


@app.get("/health")
def health() -> dict:
    snapshot = _status_snapshot()
    return {
        "ok": True,
        "version": __version__,
        "local_only": snapshot["local_only"],
        "provider": snapshot["provider"],
        "data_root": snapshot["paths"]["data_root"],
        "asset_count": snapshot["asset_count"],
        "finalized_ready": snapshot["finalized_ready"],
        "finalized_count": snapshot["finalized_count"],
    }


@app.get("/api/v1/status")
def status() -> dict:
    return _status_snapshot()


@app.get("/api/v1/search")
def search(
    q: str = "",
    limit: int = 24,
    offset: int = 0,
) -> dict:
    cat = Catalog()
    rows, total = cat.search(q, limit=limit, offset=offset)
    return {
        "query": q,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(rows) < total,
        "results": [
            _asset_json(r) for r in rows
        ],
    }


@app.get("/api/v1/library/tree")
def library_tree(
    path: str = "",
    q: str = "",
    limit: int = 200,
    offset: int = 0,
) -> dict:
    clean = normalize_virtual_path(path)
    query = q.strip()
    catalog = Catalog()
    if query:
        files, total = catalog.search(query, limit=limit, offset=offset)
        if clean:
            prefix = clean + "/"
            files = [asset for asset in files if asset.virtual_path.startswith(prefix)]
            total = len(files)
        directories = []
    else:
        directories, files, total = catalog.list_directory(
            clean, limit=limit, offset=offset
        )
    parts = clean.split("/") if clean else []
    breadcrumbs = [{"name": "素材库", "path": ""}]
    for index, part in enumerate(parts):
        breadcrumbs.append({"name": part, "path": "/".join(parts[: index + 1])})
    return {
        "path": clean,
        "breadcrumbs": breadcrumbs,
        "directories": directories,
        "files": [_asset_json(asset) for asset in files],
        "total_files": total,
        "has_more": offset + len(files) < total,
        "search_mode": bool(query),
    }


@app.post("/api/v1/library/upload")
async def upload_library_assets(
    files: list[UploadFile] = File(...),
    target_path: str = Form(""),
    relative_paths: str = Form("[]"),
) -> dict:
    settings = get_settings()
    if not library_mount_available(settings):
        raise HTTPException(503, "素材盘未挂载，已禁止写入资源库")
    if not files or len(files) > 200:
        raise HTTPException(400, "每批必须上传 1 到 200 个文件")
    catalog = Catalog(settings)
    base = _safe_library_target(settings.library_root, target_path)
    try:
        rels = json.loads(relative_paths or "[]")
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "relative_paths 不是合法 JSON") from exc
    if not isinstance(rels, list) or (rels and len(rels) != len(files)):
        raise HTTPException(400, "relative_paths 必须与文件数量一致")

    prepared = []
    for index, upload in enumerate(files):
        name = Path(upload.filename or "").name.strip()
        if not name:
            raise HTTPException(400, "文件名不能为空")
        rel = normalize_virtual_path(str(rels[index]) if rels else name)
        rel_parent = normalize_virtual_path(str(Path(rel).parent)) if "/" in rel else ""
        destination_dir = _safe_library_target(base, rel_parent)
        destination = destination_dir / name
        asset_id = f"upload:{uuid.uuid4().hex}"
        prepared.append((upload, name, asset_id, destination))
    duplicates = catalog.reserve_asset_names(
        [(name, asset_id) for _upload, name, asset_id, _destination in prepared]
    )
    existing_paths = [name for _upload, name, _asset_id, dest in prepared if dest.exists()]
    if existing_paths:
        catalog.release_asset_name_claims([item[2] for item in prepared])
        duplicate_names = {str(item.get("file_name") or "").casefold() for item in duplicates}
        duplicates.extend(
            {"file_name": name, "reason": "destination_exists"}
            for name in existing_paths
            if name.casefold() not in duplicate_names
        )
    if duplicates:
        raise HTTPException(
            409,
            {"code": "FILENAME_DUPLICATE", "message": "文件名重复，不允许添加", "duplicates": duplicates},
        )

    staged: list[tuple[Path, Path]] = []
    committed: list[Path] = []
    try:
        for upload, _name, asset_id, destination in prepared:
            destination.parent.mkdir(parents=True, exist_ok=True)
            part = destination.parent / f".{destination.name}.{asset_id.split(':', 1)[1]}.part"
            with part.open("wb") as output:
                while chunk := await upload.read(1024 * 1024):
                    output.write(chunk)
            staged.append((part, destination))
        for part, destination in staged:
            part.replace(destination)
            committed.append(destination)
        rows = []
        for _upload, name, asset_id, destination in prepared:
            stat = destination.stat()
            virtual_path = normalize_virtual_path(
                f"{target_path}/{destination.relative_to(base).as_posix()}"
            )
            rows.append(
                AssetRow(
                    asset_id=asset_id,
                    kind="library",
                    storage_key=destination.relative_to(settings.library_root).as_posix(),
                    file_name=name,
                    original_filename=name,
                    file_size=int(stat.st_size),
                    local_path=str(destination),
                    status="ready",
                    updated_at=stat.st_mtime,
                    virtual_path=virtual_path,
                )
            )
        catalog.upsert_assets(rows)
        return {"added": len(rows), "files": [_asset_json(row) for row in rows]}
    except Exception:
        for part, _destination in staged:
            part.unlink(missing_ok=True)
        for destination in committed:
            destination.unlink(missing_ok=True)
        catalog.release_asset_name_claims([item[2] for item in prepared])
        raise


@app.post("/api/v1/assets/download")
def download_assets(
    payload: AssetIdsRequest, background_tasks: BackgroundTasks
) -> FileResponse:
    return _download_archive(payload.ids, background_tasks)


@app.post("/api/v1/assets/download-ticket")
def create_download_ticket(payload: AssetIdsRequest) -> dict:
    token = uuid.uuid4().hex
    Catalog().create_download_selection(token, json.dumps(list(dict.fromkeys(payload.ids))))
    return {"token": token, "download_url": f"/api/v1/assets/download-batch/{token}"}


@app.get("/api/v1/assets/download-batch/{token}")
def download_assets_by_ticket(
    token: str, background_tasks: BackgroundTasks
) -> FileResponse:
    raw = Catalog().get_download_selection(token)
    if not raw:
        raise HTTPException(404, "下载选择已过期")
    return _download_archive(json.loads(raw), background_tasks)


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
    filename = asset.file_name or path.name
    if not inline:
        sku_code = cat.resolve_asset_sku_codes([asset]).get(asset.asset_id, "")
        filename = ensure_sku_in_filename(filename, sku_code)
    # 默认下载为 attachment；显式 inline 或预览场景走 inline
    return _serve_under_data(
        path,
        s.data_root,
        filename,
        inline=inline,
        use_accel=s.api.x_accel,
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
        use_accel=s.api.x_accel,
    )


@app.get("/api/v1/pack-rules")
def list_pack_rules() -> dict:
    store = PackRuleStore()
    return {
        "rules": [rule.to_dict() for rule in store.list()],
        "handlers": list(SUPPORTED_HANDLERS.values()),
    }


@app.post("/api/v1/pack-rules")
def create_pack_rule(payload: PackRulePayload) -> dict:
    try:
        rule = PackRuleStore().create(**payload.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return rule.to_dict()


@app.put("/api/v1/pack-rules/{rule_id}")
def update_pack_rule(rule_id: str, payload: PackRulePayload) -> dict:
    try:
        rule = PackRuleStore().update(rule_id, **payload.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not rule:
        raise HTTPException(404, "规则不存在")
    return rule.to_dict()


@app.delete("/api/v1/pack-rules/{rule_id}")
def delete_pack_rule(rule_id: str) -> dict:
    if not PackRuleStore().delete(rule_id):
        raise HTTPException(404, "规则不存在")
    return {"deleted": True, "id": rule_id}


@app.get("/api/v1/jobs")
def list_jobs(limit: int = 20) -> dict:
    global _jobs_cache_at, _jobs_cache
    now = time.monotonic()
    if limit == 20 and now - _jobs_cache_at <= 0.5:
        return _jobs_cache
    acquired = _jobs_cache_lock.acquire(blocking=_jobs_cache_at == 0.0)
    if not acquired:
        return _jobs_cache
    try:
        now = time.monotonic()
        if limit == 20 and now - _jobs_cache_at <= 0.5:
            return _jobs_cache
        result = _list_jobs_uncached(limit)
        if limit == 20:
            _jobs_cache = result
            _jobs_cache_at = now
        return result
    finally:
        _jobs_cache_lock.release()


def _list_jobs_uncached(limit: int) -> dict:
    store = JobStore()
    jobs = store.list(limit=limit)
    return {
        "jobs": [
            {
                "id": j.id,
                "status": j.status,
                "filename": j.filename,
                "created_at": j.created_at,
                "started_at": j.started_at,
                "finished_at": j.finished_at,
                "progress": j.progress,
                "error": j.error,
                "has_download": bool(j.archive_path and Path(j.archive_path).is_file()),
            }
            for j in jobs
        ]
    }


@app.post("/api/v1/jobs")
def create_job(
    file: UploadFile = File(...),
    super_dir_name: str = Form(""),
    rule_ids: str = Form(""),
) -> dict:
    s = get_settings()
    if not library_mount_available(s):
        raise HTTPException(503, "素材盘未挂载，暂不接受打包任务")
    cat = Catalog(s)
    if s.local_only and cat.count_ready_all() == 0:
        raise HTTPException(409, "统一素材库暂无可用文件，请先完成同步或本地目录索引")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".xlsx", ".xls"}:
        raise HTTPException(400, "仅支持 .xlsx / .xls")
    selected_ids = None
    if rule_ids:
        try:
            selected_ids = json.loads(rule_ids)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "rule_ids 不是合法 JSON") from exc
        if not isinstance(selected_ids, list):
            raise HTTPException(400, "rule_ids 必须是数组")
    try:
        rules = PackRuleStore(s).snapshots(selected_ids)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    incoming = s.tmp_dir / f"incoming-{uuid.uuid4().hex}{suffix}"
    try:
        with incoming.open("wb") as output:
            shutil.copyfileobj(file.file, output)
        try:
            input_rows = read_excel_rows(incoming)
        except Exception as exc:
            raise HTTPException(400, f"Excel 无法解析：{exc}") from exc
        if not input_rows:
            raise HTTPException(400, "Excel 中没有可处理的编码行")
        unique_rows, duplicate_rows = deduplicate_rows(input_rows)
        duplicate_codes = list(
            dict.fromkeys(
                item["row"].sku_code or item["row"].sku_name
                for item in duplicate_rows
            )
        )
        store = JobStore(s)
        job = store.create(
            filename=file.filename or f"input{suffix}",
            super_dir_name=super_dir_name,
            meta={
                "rules": rules,
                "input_rows": len(input_rows),
                "unique_rows": len(unique_rows),
                "duplicate_codes": duplicate_codes,
            },
            enqueue=False,
        )
        job_dir = store.job_dir(job.id)
        dest = job_dir / f"input{suffix}"
        incoming.replace(dest)
        store.enqueue(job.id)
    except HTTPException:
        incoming.unlink(missing_ok=True)
        raise
    except Exception as exc:
        incoming.unlink(missing_ok=True)
        if "store" in locals() and "job" in locals():
            store.update(
                job.id,
                status="failed",
                error=str(exc),
                progress={"percent": 0, "label": "upload failed"},
                finished=True,
            )
        raise
    return {
        "job_id": job.id,
        "input_rows": len(input_rows),
        "unique_rows": len(unique_rows),
        "duplicate_rows": len(duplicate_rows),
        "duplicate_codes": duplicate_codes,
    }


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
        "rules": (job.meta or {}).get("rules", []),
    }


@app.get("/api/v1/jobs/{job_id}/download")
def download_job(job_id: str) -> Response:
    s = get_settings()
    with _download_cache_lock:
        cached = _download_cache.get(job_id)
    if cached:
        archive_path, download_name = cached
        path = Path(archive_path)
        name = download_name
    else:
        job = JobStore(s).get(job_id)
        if not job:
            raise HTTPException(404, "任务不存在")
        if job.status != "done" or not job.archive_path:
            raise HTTPException(409, "任务未完成")
        path = Path(job.archive_path)
        name = f"{Path(job.filename or job_id).stem}_pack.zip"
        with _download_cache_lock:
            _download_cache[job_id] = (str(path), name)
    if not path.is_file():
        with _download_cache_lock:
            _download_cache.pop(job_id, None)
        raise HTTPException(404, "结果文件不存在")
    return _serve_under_data(path, s.data_root, name, use_accel=s.api.x_accel)


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
        workers=s.api.workers,
        backlog=s.api.backlog,
        limit_concurrency=s.api.limit_concurrency,
        timeout_keep_alive=30,
        access_log=False,
        log_level="info",
    )


if __name__ == "__main__":
    run()
