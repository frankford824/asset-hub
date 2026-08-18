from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from asset_hub.catalog.db import AssetRow, Catalog
from asset_hub.catalog.ignore import should_ignore
from asset_hub.config import ensure_data_dirs, get_settings

log = logging.getLogger("asset_hub.index")
BATCH_SIZE = 25


def index_library(
    catalog: Catalog,
    root: Path,
    ignore_globs: list[str],
    limit: int | None = None,
) -> int:
    if not root.is_dir():
        log.warning("library root missing: %s", root)
        return 0
    count = 0
    changed = 0
    skipped = 0
    batch: list[AssetRow] = []
    existing = catalog.library_fingerprints()
    seen: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        # prune ignored dirs
        dirnames[:] = [d for d in dirnames if not should_ignore(d, ignore_globs)]
        for name in filenames:
            if should_ignore(name, ignore_globs):
                continue
            path = Path(dirpath) / name
            try:
                st = path.stat()
            except OSError:
                continue
            rel = path.relative_to(root).as_posix()
            asset_id = f"lib:{rel}"
            seen.add(asset_id)
            before = existing.get(asset_id)
            if (
                before
                and not int(before["deleted"] or 0)
                and before["status"] == "ready"
                and before["file_name"] == name
                and int(before["file_size"] or 0) == int(st.st_size)
                and abs(float(before["updated_at"] or 0) - float(st.st_mtime)) < 0.000001
                and before["local_path"] == str(path)
                and before["virtual_path"] == rel
            ):
                count += 1
                skipped += 1
                if limit and count >= limit:
                    log.info(
                        "library index partial scanned=%s changed=%s skipped=%s",
                        count,
                        changed,
                        skipped,
                    )
                    return count
                continue
            batch.append(
                AssetRow(
                    asset_id=asset_id,
                    kind="library",
                    storage_key=rel,
                    file_name=name,
                    original_filename=name,
                    file_size=int(st.st_size),
                    local_path=str(path),
                    status="ready",
                    updated_at=st.st_mtime,
                    sku_code="",
                    sku_name="",
                    virtual_path=rel,
                )
            )
            count += 1
            changed += 1
            if len(batch) >= BATCH_SIZE:
                catalog.upsert_assets(batch)
                batch.clear()
            if limit and count >= limit:
                if batch:
                    catalog.upsert_assets(batch)
                return count
        if count and count % 5000 == 0:
            log.info("indexed library files=%s", count)
    if batch:
        catalog.upsert_assets(batch)
    tombstoned = catalog.tombstone_library_assets(
        sorted(set(existing) - seen), batch_size=BATCH_SIZE
    )
    log.info(
        "library index scanned=%s changed=%s skipped=%s tombstoned=%s",
        count,
        changed,
        skipped,
        tombstoned,
    )
    return count


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = ensure_data_dirs()
    catalog = Catalog(settings)
    t0 = time.time()
    n = index_library(catalog, settings.library_root, settings.sync.ignore_globs)
    log.info("library index done files=%s elapsed=%.1fs", n, time.time() - t0)


if __name__ == "__main__":
    run()
