from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path

import httpx

from asset_hub.catalog.db import AssetRow, Catalog, local_path_for_kind
from asset_hub.catalog.ignore import should_ignore
from asset_hub.config import ensure_data_dirs, get_settings
from asset_hub.sync.provider import (
    DownloadTicket,
    SyncProvider,
    build_provider,
    copy_mock_ticket,
)

log = logging.getLogger("asset_hub.sync")
SHA256_RE = re.compile(r"^(?:sha256:)?([0-9a-fA-F]{64})$")


def _download_ready_ticket(
    destination: Path,
    asset: AssetRow,
    ticket: DownloadTicket,
    settings,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".part")
    tmp.unlink(missing_ok=True)
    try:
        if not copy_mock_ticket(ticket, tmp):
            if not ticket.download_url:
                raise ValueError("ready ticket has no download_url")
            if ticket.expires_at and ticket.expires_at <= time.time() + 5:
                raise ValueError("download ticket is expired or about to expire")
            with httpx.stream(
                "GET",
                ticket.download_url,
                timeout=settings.http.timeout_sec,
                follow_redirects=True,
            ) as response:
                response.raise_for_status()
                with tmp.open("wb") as output:
                    for chunk in response.iter_bytes(1024 * 1024):
                        output.write(chunk)
        actual_size = tmp.stat().st_size
        if actual_size != asset.file_size:
            raise ValueError(
                f"download size mismatch task_asset_id={asset.task_asset_id} "
                f"got={actual_size} want={asset.file_size}"
            )
        _verify_whole_hash(tmp, asset.whole_hash)
        tmp.replace(destination)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _verify_whole_hash(path: Path, whole_hash: str) -> None:
    value = (whole_hash or "").strip()
    if not value:
        return
    match = SHA256_RE.fullmatch(value)
    if not match:
        # The upstream field predates a declared algorithm. Only a declared or
        # unambiguous SHA-256 can be verified locally.
        return
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest().lower() != match.group(1).lower():
        raise ValueError(f"whole_hash mismatch for {path.name}")


def _ticket_matches_local(asset: AssetRow, ticket: DownloadTicket) -> bool:
    if asset.status != "ready" or not asset.local_path:
        return False
    path = Path(asset.local_path)
    if not path.is_file() or path.stat().st_size != asset.file_size:
        return False
    if ticket.etag and ticket.etag != asset.etag:
        return False
    if ticket.crc64_ecma and ticket.crc64_ecma != asset.crc64_ecma:
        return False
    if ticket.whole_hash and asset.whole_hash and ticket.whole_hash != asset.whole_hash:
        return False
    return bool(asset.etag or asset.crc64_ecma or asset.whole_hash)


def _validate_ready_ticket(asset: AssetRow, ticket: DownloadTicket) -> None:
    if ticket.storage_key != asset.storage_key:
        raise ValueError("ticket storage_key differs from manifest")
    if ticket.expected_size != asset.file_size or ticket.actual_size != asset.file_size:
        raise ValueError("ready ticket size differs from manifest")
    if not ticket.download_url:
        raise ValueError("ready ticket has no download_url")
    if ticket.whole_hash and asset.whole_hash and ticket.whole_hash != asset.whole_hash:
        raise ValueError("ticket whole_hash differs from manifest")


def _process_ticket(
    catalog: Catalog,
    asset: AssetRow,
    ticket: DownloadTicket,
    settings,
    stats: dict,
) -> bool:
    task_asset_id = int(asset.task_asset_id or 0)
    if ticket.task_asset_id != task_asset_id:
        catalog.mark_task_asset_status(
            task_asset_id,
            "error",
            retryable=False,
            error="ticket task_asset_id mismatch",
        )
        stats["error"] += 1
        return False
    if ticket.status == "not_current":
        catalog.mark_task_asset_tombstone(task_asset_id)
        stats["not_current"] += 1
        stats["tombstone"] += 1
        return False
    if ticket.status == "missing":
        catalog.mark_task_asset_status(
            task_asset_id, "missing", retryable=False, error="OSS object missing"
        )
        stats["missing"] += 1
        return False
    if ticket.status == "size_mismatch":
        catalog.mark_task_asset_status(
            task_asset_id,
            "size_mismatch",
            etag=ticket.etag,
            crc64_ecma=ticket.crc64_ecma,
            retryable=False,
            error=(
                f"OSS size mismatch expected={ticket.expected_size} "
                f"actual={ticket.actual_size}"
            ),
        )
        stats["size_mismatch"] += 1
        return False
    if ticket.status == "error":
        catalog.mark_task_asset_status(
            task_asset_id,
            "error",
            retryable=ticket.retryable,
            error=ticket.error_message or "upstream ticket error",
        )
        stats["error"] += 1
        if ticket.retryable:
            stats["retryable_error"] += 1
        return False
    try:
        _validate_ready_ticket(asset, ticket)
    except Exception as exc:
        catalog.mark_task_asset_status(
            task_asset_id,
            "error",
            retryable=False,
            error=str(exc),
        )
        stats["error"] += 1
        log.error("invalid ready ticket task_asset_id=%s error=%s", task_asset_id, exc)
        return False
    try:
        if _ticket_matches_local(asset, ticket):
            destination = Path(asset.local_path)
            stats["skipped"] += 1
        else:
            destination = local_path_for_kind(
                settings, "finalized", str(task_asset_id), asset.file_name
            )
            _download_ready_ticket(destination, asset, ticket, settings)
            stats["written"] += 1
        catalog.mark_task_asset_status(
            task_asset_id,
            "ready",
            local_path=str(destination),
            etag=ticket.etag,
            crc64_ecma=ticket.crc64_ecma,
            retryable=False,
            error="",
        )
        stats["ready"] += 1
        return True
    except Exception as exc:
        catalog.mark_task_asset_status(
            task_asset_id,
            "error",
            retryable=True,
            error=str(exc),
        )
        stats["error"] += 1
        stats["retryable_error"] += 1
        log.exception("download failed task_asset_id=%s", task_asset_id)
        return False


def sync_once(provider: SyncProvider | None = None) -> dict:
    settings = ensure_data_dirs()
    catalog = Catalog(settings)
    state = catalog.get_sync_state("finalized")
    stats = {
        "manifest_modified": False,
        "manifest_objects": 0,
        "manifest_items": 0,
        "requested": 0,
        "ready": 0,
        "written": 0,
        "skipped": 0,
        "missing": 0,
        "size_mismatch": 0,
        "not_current": 0,
        "error": 0,
        "retryable_error": 0,
        "tombstone": 0,
        "ready_for_pack": False,
    }
    unsupported_kinds = [kind for kind in settings.sync.kinds if kind != "finalized"]
    if unsupported_kinds:
        message = (
            "manifest/ticket provider only supports finalized; unsupported sync kinds: "
            + ", ".join(unsupported_kinds)
        )
        stats["error"] = 1
        catalog.set_sync_state(
            "finalized",
            ready=False,
            error=message,
            stats_json=json.dumps(stats, ensure_ascii=False),
        )
        return stats
    try:
        provider = provider or build_provider(settings)
        manifest = provider.get_manifest(str(state.get("etag") or ""))
    except Exception as exc:
        stats["error"] = 1
        catalog.set_sync_state(
            "finalized",
            ready=False,
            error=str(exc),
            stats_json=json.dumps(stats, ensure_ascii=False),
        )
        log.exception("manifest request failed")
        return stats

    manifest_changed = not manifest.not_modified
    if manifest_changed:
        try:
            if not manifest.manifest_id:
                raise ValueError("modified manifest is missing manifest_id")
            filtered_items = [
                item
                for item in manifest.items
                if not should_ignore(item.file_name, settings.sync.ignore_globs)
                and not should_ignore(item.original_filename, settings.sync.ignore_globs)
            ]
            snapshot_stats = catalog.apply_finalized_manifest(
                filtered_items, manifest.manifest_id
            )
        except Exception as exc:
            stats["error"] = 1
            catalog.set_sync_state(
                "finalized",
                etag="",
                ready=False,
                error=str(exc),
                stats_json=json.dumps(stats, ensure_ascii=False),
            )
            log.exception("manifest apply failed")
            return stats
        stats["manifest_modified"] = True
        stats["manifest_objects"] = snapshot_stats["objects"]
        stats["manifest_items"] = snapshot_stats["items"]
        stats["tombstone"] += snapshot_stats["exited"]
        state_etag = manifest.etag
        manifest_id = manifest.manifest_id
        catalog.set_sync_state(
            "finalized",
            etag=state_etag,
            manifest_id=manifest_id,
            ready=False,
            error="",
        )
    else:
        state_etag = manifest.etag or str(state.get("etag") or "")
        manifest_id = str(state.get("manifest_id") or manifest.manifest_id or "")
        if not manifest_id:
            stats["error"] = 1
            catalog.set_sync_state(
                "finalized",
                ready=False,
                etag="",
                error="304 received without saved manifest state",
                stats_json=json.dumps(stats, ensure_ascii=False),
            )
            return stats
        stats["manifest_objects"] = len(catalog.list_finalized_assets())
        stats["manifest_items"] = len(catalog.list_finalized_items())

    verify_due = (
        time.time() - float(state.get("last_verified_at") or 0)
        >= settings.sync.verify_interval_sec
    )
    candidates = catalog.ticket_candidates(
        verify_all=verify_due, include_nonready=manifest_changed
    )
    stats["requested"] = len(candidates)
    force_manifest_refresh = False
    ticket_batches_failed = False
    for offset in range(0, len(candidates), settings.sync.ticket_batch_size):
        batch = candidates[offset : offset + settings.sync.ticket_batch_size]
        ids = [int(asset.task_asset_id or 0) for asset in batch]
        try:
            tickets = provider.get_download_tickets(ids)
            ticket_by_id = {ticket.task_asset_id: ticket for ticket in tickets}
            extras = set(ticket_by_id) - set(ids)
            if extras:
                raise ValueError(f"ticket response contains unexpected IDs: {sorted(extras)}")
        except Exception as exc:
            ticket_batches_failed = True
            stats["error"] += len(batch)
            stats["retryable_error"] += len(batch)
            for asset in batch:
                catalog.mark_task_asset_status(
                    int(asset.task_asset_id or 0),
                    "error",
                    retryable=True,
                    error=str(exc),
                )
            log.exception("ticket batch failed ids=%s", ids)
            continue
        for asset in batch:
            task_asset_id = int(asset.task_asset_id or 0)
            ticket = ticket_by_id.get(task_asset_id)
            if ticket is None:
                stats["error"] += 1
                stats["retryable_error"] += 1
                catalog.mark_task_asset_status(
                    task_asset_id,
                    "error",
                    retryable=True,
                    error="ticket response omitted requested task_asset_id",
                )
                continue
            success = _process_ticket(catalog, asset, ticket, settings, stats)
            if not success and ticket.status == "not_current":
                force_manifest_refresh = True

    complete = (
        not force_manifest_refresh
        and not ticket_batches_failed
        and catalog.finalized_cache_complete(manifest_id)
    )
    stats["ready_for_pack"] = complete
    failures = (
        stats["missing"]
        + stats["size_mismatch"]
        + stats["not_current"]
        + stats["error"]
    )
    error_summary = "" if complete else f"finalized sync incomplete: {failures} failures"
    verified_all = verify_due and len(candidates) == stats["manifest_objects"]
    if manifest_changed and len(candidates) == stats["manifest_objects"]:
        verified_all = True
    catalog.set_sync_state(
        "finalized",
        etag="" if force_manifest_refresh else state_etag,
        manifest_id=manifest_id,
        ready=complete,
        error=error_summary,
        stats_json=json.dumps(stats, ensure_ascii=False),
        success=complete,
        verified=verified_all,
    )
    log.info("sync finalized stats=%s", stats)
    return stats


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    stats = sync_once()
    if not stats["ready_for_pack"]:
        raise SystemExit(1)


if __name__ == "__main__":
    run()
