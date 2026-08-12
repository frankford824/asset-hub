from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from asset_hub.catalog.db import AssetRow, Catalog
from asset_hub.catalog.ignore import should_ignore
from asset_hub.config import ensure_data_dirs, get_settings

log = logging.getLogger("asset_hub.index")


def index_library(catalog: Catalog, root: Path, ignore_globs: list[str], limit: int | None = None) -> int:
    if not root.is_dir():
        log.warning("library root missing: %s", root)
        return 0
    count = 0
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
            catalog.upsert_asset(
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
                )
            )
            count += 1
            if limit and count >= limit:
                return count
        if count and count % 5000 == 0:
            log.info("indexed library files=%s", count)
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
