from __future__ import annotations

import logging
import time
from pathlib import Path

from asset_hub.catalog.db import Catalog
from asset_hub.config import ensure_data_dirs
from asset_hub.jobs import JobStore
from asset_hub.pack.excel import match_assets_for_rows, read_excel_rows
from asset_hub.pack.ziputil import zip_paths

log = logging.getLogger("asset_hub.worker")


def process_job(store: JobStore, catalog: Catalog, job_id: str) -> None:
    settings = store.settings
    job = store.get(job_id)
    if not job:
        return
    job_dir = store.job_dir(job_id)
    excel_path = None
    for p in job_dir.iterdir():
        if p.suffix.lower() in {".xlsx", ".xls"}:
            excel_path = p
            break
    if not excel_path:
        store.update(
            job_id,
            status="failed",
            error="缺少 Excel 输入",
            progress={"percent": 0, "label": "failed"},
            finished=True,
        )
        return

    try:
        if settings.local_only and not catalog.is_finalized_ready():
            # still allow packing available assets, but warn via progress
            store.update(
                job_id,
                progress={"percent": 5, "label": "finalized 未完全 ready，按已缓存匹配"},
            )
        store.update(job_id, progress={"percent": 10, "label": "解析 Excel"})
        rows = read_excel_rows(excel_path)
        matched, missing = match_assets_for_rows(catalog, rows)
        store.update(
            job_id,
            progress={
                "percent": 30,
                "label": f"匹配 {len(matched)} 行 / 缺 {len(missing)} 行",
                "matched": len(matched),
                "missing": len(missing),
            },
        )
        pairs: list[tuple[Path, str]] = []
        prefix = job.super_dir_name.strip() or "pack"
        for block in matched:
            row = block["row"]
            for asset in block["assets"]:
                src = Path(asset.local_path)
                if not src.is_file():
                    continue
                arc = f"{prefix}/{row.sku_code or row.sku_name or row.row_index}/{src.name}"
                pairs.append((src, arc))
        if not pairs:
            store.update(
                job_id,
                status="failed",
                error="没有可打包的本地终稿文件（local_only）",
                progress={"percent": 0, "label": "no local files"},
                finished=True,
            )
            return
        store.update(job_id, progress={"percent": 60, "label": f"打包 {len(pairs)} 个文件"})
        archive = job_dir / "result.zip"
        zip_paths(pairs, archive)
        store.update(
            job_id,
            status="done",
            archive_path=str(archive),
            progress={"percent": 100, "label": "done", "files": len(pairs)},
            finished=True,
        )
        log.info("job %s done files=%s", job_id, len(pairs))
    except Exception as e:
        log.exception("job failed %s", job_id)
        store.update(
            job_id,
            status="failed",
            error=str(e),
            progress={"percent": 0, "label": "failed"},
            finished=True,
        )


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = ensure_data_dirs()
    catalog = Catalog(settings)
    store = JobStore(settings)
    log.info("worker started jobs_dir=%s", settings.jobs_dir)
    while True:
        job = store.claim_next()
        if not job:
            time.sleep(1)
            continue
        log.info("claimed job %s", job.id)
        process_job(store, catalog, job.id)


if __name__ == "__main__":
    run()
