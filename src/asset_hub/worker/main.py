from __future__ import annotations

import logging
import multiprocessing
import os
import re
import signal
import time
from collections import Counter, OrderedDict
from pathlib import Path

from asset_hub.catalog.db import Catalog
from asset_hub.config import ensure_data_dirs
from asset_hub.jobs import JobStore
from asset_hub.pack.excel import (
    ExcelRow,
    deduplicate_rows,
    match_assets_for_rows,
    read_excel_rows,
)
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
        input_rows = read_excel_rows(excel_path)
        unique_rows, duplicate_rows = deduplicate_rows(input_rows)
        rows = input_rows
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
            f"收到 {len(input_rows)} 行 · 唯一 {len(unique_rows)} 个编码 · "
            f"重复 {len(duplicate_rows)} 行按次输出 · "
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
                "input_rows": len(input_rows),
                "unique_rows": len(unique_rows),
                "duplicate_rows": len(duplicate_rows),
            },
        )

        phase_started = time.perf_counter()
        prefix = _safe_component(job.super_dir_name, "pack")
        generated_dir = job_dir / "generated"
        generated_dir.mkdir(exist_ok=True)
        pairs: list[tuple[Path, str]] = []
        selection_lines = [
            "统一素材库选择说明",
            f"输入编码行：{len(input_rows)}",
            f"唯一编码：{len(unique_rows)}",
            f"重复行：{len(duplicate_rows)}（不合并，按出现次数分别输出）",
            "本任务采用的规则：" + "、".join(
                str(rule.get("name"))
                for rule in ((job.meta or {}).get("rules") or [])
                if rule.get("name")
            ),
            "",
        ]
        for duplicate in duplicate_rows:
            row = duplicate["row"]
            selection_lines.append(
                f"第 {row.row_index} 行 · {row.sku_code or row.sku_name} · "
                f"与第 {duplicate['first_row_index']} 行编码重复，保留为独立商品单位"
            )
        if duplicate_rows:
            selection_lines.append("")

        for group_index, (order_id, group_rows) in enumerate(
            _group_rows(rows, handlers).items(), start=1
        ):
            group_skus = [
                _safe_component(row.sku_code or row.sku_name, f"row{row.row_index}")
                for row in group_rows
            ]
            sku_totals = Counter(group_skus)
            sku_seen: Counter[str] = Counter()
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
                sku_seen[sku] += 1
                instance_suffix = (
                    f"_{sku_seen[sku]}" if sku_totals[sku] > 1 else ""
                )
                copies = row.quantity if "repeat_quantity" in handlers else 1
                product_groups: OrderedDict[str, list] = OrderedDict()
                for asset in assets:
                    parent = Path(asset.virtual_path).parent.name if asset.virtual_path else ""
                    descriptive_parent = (
                        parent
                        if parent
                        and sku.casefold() in parent.casefold()
                        and parent.casefold() != sku.casefold()
                        else ""
                    )
                    if not descriptive_parent and (
                        len(assets) > 1 or sku.casefold() not in asset.file_name.casefold()
                    ):
                        product_name = (asset.sku_name or "").strip()
                        if product_name:
                            descriptive_parent = (
                                product_name
                                if sku.casefold() in product_name.casefold()
                                else f"{sku}——{product_name}"
                            )
                    product_groups.setdefault(descriptive_parent, []).append(asset)
                # A product description is only a naming hint. It must not turn a
                # single finalized file into a one-file directory (for example,
                # an upstream ``set`` revision that currently contains one item).
                should_group = len(assets) > 1
                if should_group and "rename_sku_sequence" in handlers:
                    for copy_index in range(1, copies + 1):
                        for product_parent, grouped_assets in product_groups.items():
                            folder = _safe_component(product_parent, sku)
                            folder += instance_suffix
                            if copies > 1:
                                folder += f"_copy{copy_index}"
                            for asset in grouped_assets:
                                src = Path(asset.local_path)
                                pairs.append((src, f"{base}/{folder}/{src.name}"))
                elif "rename_sku_sequence" in handlers:
                    src = Path(assets[0].local_path)
                    for copy_index in range(1, copies + 1):
                        suffix = instance_suffix
                        if copies > 1:
                            suffix += f"_copy{copy_index}"
                        output_name = f"{src.stem}{suffix}{src.suffix.lower()}"
                        pairs.append((src, f"{base}/{output_name}"))
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
                "input_rows": len(input_rows),
                "unique_rows": len(unique_rows),
                "duplicate_rows": len(duplicate_rows),
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
    # Install handlers only in the supervisor. With systemd KillMode=mixed,
    # children finish their current job and observe stop_event normally.
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
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
        deadline = time.monotonic() + 290
        for process in processes:
            process.join(timeout=max(0.0, deadline - time.monotonic()))
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    run()
