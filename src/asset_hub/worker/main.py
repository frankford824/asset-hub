from __future__ import annotations

import logging
import multiprocessing
import os
import re
import signal
import time
from collections import OrderedDict
from pathlib import Path

from asset_hub.catalog.db import Catalog
from asset_hub.config import ensure_data_dirs
from asset_hub.jobs import JobStore
from asset_hub.pack.excel import ExcelRow, match_assets_for_rows, read_excel_rows
from asset_hub.pack.ziputil import zip_paths

log = logging.getLogger("asset_hub.worker")


def _safe_component(value: str, fallback: str) -> str:
    clean = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", (value or "").strip())
    return clean.strip(". ") or fallback


def _rule_handlers(job) -> set[str]:
    rules = (job.meta or {}).get("rules") or []
    if not rules:
        # Backward-compatible jobs created before rule snapshots existed.
        return {"prefer_current", "library_fallback", "selection_report", "fast_zip"}
    return {str(rule.get("handler") or "") for rule in rules if rule.get("handler")}


def _group_rows(rows: list[ExcelRow], handlers: set[str]) -> OrderedDict[str, list[ExcelRow]]:
    groups: OrderedDict[str, list[ExcelRow]] = OrderedDict()
    for row in rows:
        key = row.order_id if "group_by_order" in handlers and row.order_id else ""
        groups.setdefault(key, []).append(row)
    return groups


def process_job(store: JobStore, catalog: Catalog, job_id: str) -> None:
    total_started = time.perf_counter()
    timings: dict[str, float] = {}
    settings = store.settings
    job = store.get(job_id)
    if not job:
        return
    job_dir = store.job_dir(job_id)
    excel_path = next(
        (path for path in job_dir.iterdir() if path.suffix.lower() in {".xlsx", ".xls"}),
        None,
    )
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
        handlers = _rule_handlers(job)
        if settings.local_only and not catalog.is_finalized_ready():
            store.update(
                job_id,
                progress={"percent": 5, "label": "素材仍在同步，按当前可用内容匹配"},
            )
        store.update(job_id, progress={"percent": 10, "label": "解析 Excel"})
        phase_started = time.perf_counter()
        rows = read_excel_rows(excel_path)
        timings["parse_s"] = round(time.perf_counter() - phase_started, 4)
        phase_started = time.perf_counter()
        matched, missing = match_assets_for_rows(
            catalog, rows, rule_handlers=handlers
        )
        timings["match_s"] = round(time.perf_counter() - phase_started, 4)
        matched_by_row = {block["row"].row_index: block for block in matched}
        missing_by_row = {item["row"].row_index: item for item in missing}
        preferred_rows = sum(
            block["selection_policy"] == "preferred_current" for block in matched
        )
        fallback_rows = sum(
            block["selection_policy"] == "library_fallback" for block in matched
        )
        summary = (
            f"匹配 {len(matched)} 行 · 优选 {preferred_rows} 行 · "
            f"兜底 {fallback_rows} 行 · 缺失 {len(missing)} 行"
        )
        store.update(
            job_id,
            progress={
                "percent": 30,
                "label": summary,
                "matched": len(matched),
                "missing": len(missing),
                "preferred_rows": preferred_rows,
                "fallback_rows": fallback_rows,
            },
        )

        phase_started = time.perf_counter()
        prefix = _safe_component(job.super_dir_name, "pack")
        generated_dir = job_dir / "generated"
        generated_dir.mkdir(exist_ok=True)
        pairs: list[tuple[Path, str]] = []
        selection_lines = [
            "统一素材库选择说明",
            "本任务采用的规则：" + "、".join(
                str(rule.get("name"))
                for rule in ((job.meta or {}).get("rules") or [])
                if rule.get("name")
            ),
            "",
        ]

        for group_index, (order_id, group_rows) in enumerate(
            _group_rows(rows, handlers).items(), start=1
        ):
            group_missing = [
                missing_by_row[row.row_index]
                for row in group_rows
                if row.row_index in missing_by_row
            ]
            group_name = _safe_component(order_id, f"订单_{group_index}") if order_id else ""
            address = next((row.address for row in group_rows if row.address), "")
            if group_name and "mark_sensitive" in handlers and "*" in address:
                group_name += "_【敏感】"
            if group_name and group_missing and "mark_incomplete" in handlers:
                group_name += "_未找全"
            base = f"{prefix}/{group_name}" if group_name else prefix

            if address and "write_address" in handlers:
                path = generated_dir / f"address-{group_index}.txt"
                path.write_text(address, encoding="utf-8")
                pairs.append((path, f"{base}/地址.txt"))

            sku_counters: dict[str, int] = {}
            for row in group_rows:
                block = matched_by_row.get(row.row_index)
                if not block:
                    item = missing_by_row.get(row.row_index)
                    if item:
                        selection_lines.append(
                            f"第 {row.row_index} 行 · {item['query'] or '未提供检索词'} · {item['reason']}"
                        )
                    continue
                assets = [asset for asset in block["assets"] if Path(asset.local_path).is_file()]
                if not assets:
                    continue
                sku = _safe_component(row.sku_code or row.sku_name, f"row{row.row_index}")
                copies = row.quantity if "repeat_quantity" in handlers else 1
                if len(assets) > 1 and "rename_sku_sequence" in handlers:
                    for copy_index in range(1, copies + 1):
                        folder = f"{sku}_row{row.row_index}"
                        if copies > 1:
                            folder += f"_{copy_index}"
                        for asset in assets:
                            src = Path(asset.local_path)
                            pairs.append((src, f"{base}/{folder}/{src.name}"))
                elif "rename_sku_sequence" in handlers:
                    src = Path(assets[0].local_path)
                    start = sku_counters.get(sku, 0)
                    for number in range(start + 1, start + copies + 1):
                        pairs.append((src, f"{base}/{sku}_{number}{src.suffix.lower()}"))
                    sku_counters[sku] = start + copies
                else:
                    for asset in assets:
                        src = Path(asset.local_path)
                        for copy_index in range(1, copies + 1):
                            suffix = f"_{copy_index}" if copies > 1 else ""
                            pairs.append(
                                (src, f"{base}/{sku}/{src.stem}{suffix}{src.suffix}")
                            )
                selection_lines.append(
                    f"第 {row.row_index} 行 · {sku} · {block['selection_label']} · "
                    f"{len(assets)} 个文件 × {copies}"
                )

            if group_missing and "missing_report" in handlers:
                path = generated_dir / f"missing-{group_index}.txt"
                path.write_text(
                    "".join(
                        f"SKU: {item['query']} | {item['reason']}\n"
                        for item in group_missing
                    ),
                    encoding="utf-8",
                )
                pairs.append((path, f"{base}/未找到编码.txt"))

        if "selection_report" in handlers:
            report = generated_dir / "素材选择说明.txt"
            report.write_text("\n".join(selection_lines) + "\n", encoding="utf-8")
            pairs.append((report, f"{prefix}/素材选择说明.txt"))
        if not pairs:
            store.update(
                job_id,
                status="failed",
                error="统一素材库中没有可打包的本地文件",
                progress={"percent": 0, "label": "no local files"},
                finished=True,
            )
            return

        timings["plan_s"] = round(time.perf_counter() - phase_started, 4)

        store.update(job_id, progress={"percent": 60, "label": f"打包 {len(pairs)} 个文件"})
        archive = job_dir / "result.zip"
        phase_started = time.perf_counter()
        zip_paths(pairs, archive, fast_media="fast_zip" in handlers)
        timings["zip_s"] = round(time.perf_counter() - phase_started, 4)
        timings["total_s"] = round(time.perf_counter() - total_started, 4)
        store.update(
            job_id,
            status="done",
            archive_path=str(archive),
            progress={
                "percent": 100,
                "label": summary,
                "files": len(pairs),
                "matched": len(matched),
                "missing": len(missing),
                "preferred_rows": preferred_rows,
                "fallback_rows": fallback_rows,
                "archive_bytes": archive.stat().st_size,
                "timings": timings,
            },
            finished=True,
        )
        log.info(
            "job %s done files=%s bytes=%s timings=%s rules=%s",
            job_id,
            len(pairs),
            archive.stat().st_size,
            timings,
            sorted(handlers),
        )
    except Exception as exc:
        log.exception("job failed %s", job_id)
        store.update(
            job_id,
            status="failed",
            error=str(exc),
            progress={"percent": 0, "label": "failed"},
            finished=True,
        )


def _worker_loop(worker_id: int, stop_event=None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = ensure_data_dirs()
    catalog = Catalog(settings)
    store = JobStore(settings)
    log.info(
        "worker started id=%s pid=%s jobs_dir=%s",
        worker_id,
        os.getpid(),
        settings.jobs_dir,
    )
    while stop_event is None or not stop_event.is_set():
        job = store.claim_next()
        if not job:
            if stop_event is not None:
                stop_event.wait(0.1)
            else:
                time.sleep(0.1)
            continue
        log.info("worker=%s claimed job %s", worker_id, job.id)
        process_job(store, catalog, job.id)


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = ensure_data_dirs()
    store = JobStore(settings)
    requeued = store.requeue_interrupted()
    worker_count = max(1, int(settings.pack_workers))
    if worker_count == 1:
        if requeued:
            log.warning("requeued interrupted jobs=%s", requeued)
        _worker_loop(1)
        return

    context = multiprocessing.get_context("fork")
    stop_event = context.Event()

    def request_stop(_signum, _frame) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    processes = [
        context.Process(
            target=_worker_loop,
            args=(worker_id, stop_event),
            name=f"asset-pack-{worker_id}",
        )
        for worker_id in range(1, worker_count + 1)
    ]
    for process in processes:
        process.start()
    log.warning(
        "worker pool started processes=%s requeued=%s",
        worker_count,
        requeued,
    )
    failed = False
    try:
        while not stop_event.is_set():
            for process in processes:
                process.join(timeout=0.25)
                if process.exitcode not in (None, 0):
                    failed = True
                    stop_event.set()
                    break
    finally:
        stop_event.set()
        for process in processes:
            process.join(timeout=300)
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    run()
