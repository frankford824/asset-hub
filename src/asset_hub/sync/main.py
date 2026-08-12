from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import httpx

from asset_hub.catalog.db import AssetRow, Catalog, local_path_for_kind
from asset_hub.catalog.ignore import should_ignore
from asset_hub.config import ensure_data_dirs, get_settings
from asset_hub.sync.provider import SyncItem, build_provider

log = logging.getLogger("asset_hub.sync")


def _download_to(path: Path, item: SyncItem, provider, settings) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    if tmp.exists():
        tmp.unlink()

    # mock: copy seed file
    if item.mock_seed and Path(item.mock_seed).is_file():
        shutil.copyfile(item.mock_seed, tmp)
    else:
        info = provider.resolve_download(item.storage_key)
        url = info.url
        if url.startswith("mock://"):
            raise FileNotFoundError(f"mock 无种子文件: {item.asset_id}")
        with httpx.stream("GET", url, timeout=settings.http.timeout_sec) as resp:
            resp.raise_for_status()
            with tmp.open("wb") as f:
                for chunk in resp.iter_bytes(1024 * 1024):
                    f.write(chunk)

    size = tmp.stat().st_size
    if item.file_size and size != item.file_size:
        tmp.unlink(missing_ok=True)
        raise ValueError(f"size mismatch asset={item.asset_id} got={size} want={item.file_size}")
    tmp.replace(path)


def sync_once() -> dict:
    settings = ensure_data_dirs()
    catalog = Catalog(settings)
    provider = build_provider(settings)
    stats = {
        "fetched": 0,
        "written": 0,
        "skipped": 0,
        "tombstone": 0,
        "failed": 0,
        "kinds": {},
    }

    for kind in settings.sync.kinds:
        st = catalog.get_sync_state(kind)
        cursor = st.get("cursor") or ""
        kind_stats = {"fetched": 0, "written": 0, "skipped": 0, "failed": 0, "tombstone": 0}
        pages = 0
        while True:
            pages += 1
            result = provider.list_items(kind, cursor, limit=100)
            if not result.items and not result.next_cursor:
                break
            for item in result.items:
                kind_stats["fetched"] += 1
                stats["fetched"] += 1
                if should_ignore(item.file_name, settings.sync.ignore_globs) or should_ignore(
                    item.original_filename, settings.sync.ignore_globs
                ):
                    kind_stats["skipped"] += 1
                    stats["skipped"] += 1
                    continue
                if item.deleted:
                    catalog.mark_tombstone(item.asset_id)
                    kind_stats["tombstone"] += 1
                    stats["tombstone"] += 1
                    continue
                try:
                    dest = local_path_for_kind(
                        settings, item.kind or kind, item.asset_id, item.file_name
                    )
                    need = True
                    if dest.is_file() and dest.stat().st_size == item.file_size:
                        need = False
                    if need:
                        _download_to(dest, item, provider, settings)
                        kind_stats["written"] += 1
                        stats["written"] += 1
                    catalog.upsert_asset(
                        AssetRow(
                            asset_id=item.asset_id,
                            kind=item.kind or kind,
                            storage_key=item.storage_key,
                            file_name=item.file_name,
                            original_filename=item.original_filename or item.file_name,
                            file_size=item.file_size or dest.stat().st_size,
                            etag=item.etag,
                            whole_hash=item.whole_hash,
                            sku_code=item.sku_code,
                            sku_name=item.sku_name,
                            local_path=str(dest),
                            status="ready",
                            updated_at=item.updated_at,
                        )
                    )
                except Exception as e:
                    log.exception("sync item failed %s", item.asset_id)
                    kind_stats["failed"] += 1
                    stats["failed"] += 1
                    catalog.upsert_asset(
                        AssetRow(
                            asset_id=item.asset_id,
                            kind=item.kind or kind,
                            storage_key=item.storage_key,
                            file_name=item.file_name,
                            original_filename=item.original_filename or item.file_name,
                            file_size=item.file_size,
                            etag=item.etag,
                            whole_hash=item.whole_hash,
                            sku_code=item.sku_code,
                            sku_name=item.sku_name,
                            local_path="",
                            status="failed",
                            updated_at=item.updated_at,
                        )
                    )
                    catalog.set_sync_state(kind, error=str(e))
            cursor = result.next_cursor
            catalog.set_sync_state(kind, cursor=cursor or cursor)
            if not result.next_cursor:
                break
            if pages > 10000:
                log.error("sync page safety break kind=%s", kind)
                break

        ready = kind_stats["failed"] == 0 and catalog.count_ready(kind) > 0
        # mock always ready after first full pass even if some failed? prefer ready when written+existing
        if catalog.count_ready(kind) > 0 and kind_stats["failed"] == 0:
            ready = True
        elif catalog.count_ready(kind) > 0:
            ready = True  # partial ok for local_only pack of available
        catalog.set_sync_state(
            kind,
            cursor=cursor,
            ready=ready,
            error="",
            stats_json=json.dumps(kind_stats, ensure_ascii=False),
            success=True,
        )
        stats["kinds"][kind] = kind_stats
        log.info("sync kind=%s stats=%s ready=%s", kind, kind_stats, ready)

    return stats


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    stats = sync_once()
    log.info("sync done %s", stats)


if __name__ == "__main__":
    run()
