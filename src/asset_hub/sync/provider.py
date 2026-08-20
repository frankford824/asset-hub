from __future__ import annotations

import hashlib
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Protocol

import httpx

from asset_hub.config import Settings


MANIFEST_PATH = "/v1/integration/asset-sync/finalized/manifest"
TICKETS_PATH = "/v1/integration/asset-sync/finalized/download-tickets"
EXTERNAL_MANIFEST_PATH = "/v1/integration/asset-sync/external-current/manifest"
EXTERNAL_HEAD_PATH = "/v1/integration/asset-sync/external-current/head"
EXTERNAL_CHANGES_PATH = "/v1/integration/asset-sync/external-current/changes"
EXTERNAL_TICKETS_PATH = "/v1/integration/asset-sync/external-current/download-tickets"


def _timestamp(value: object) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text).timestamp()


def _etag_manifest_id(value: str) -> str:
    candidate = value.strip()
    if candidate.startswith("W/"):
        candidate = candidate[2:].strip()
    return candidate.strip('"')


@dataclass(frozen=True)
class ManifestItem:
    task_asset_id: int
    storage_key: str
    file_name: str
    original_filename: str
    file_size: int
    whole_hash: str
    asset_updated_at: float
    format: str
    mime_type: str
    group_id: int
    revision_id: int
    revision_mode: str
    finalized_at: float
    task_id: int
    task_no: str
    scope_kind: str
    sku_code: str
    product_name: str
    revision_item_id: int
    sort_order: int
    item_name: str
    mock_seed: str = ""


@dataclass(frozen=True)
class ManifestResult:
    items: list[ManifestItem] = field(default_factory=list)
    manifest_id: str = ""
    etag: str = ""
    generated_at: float = 0.0
    object_count: int = 0
    not_modified: bool = False


@dataclass(frozen=True)
class DownloadTicket:
    task_asset_id: int
    status: str
    storage_key: str = ""
    file_name: str = ""
    expected_size: int | None = None
    actual_size: int | None = None
    etag: str = ""
    crc64_ecma: str = ""
    whole_hash: str = ""
    download_url: str = ""
    expires_at: float = 0.0
    retryable: bool = False
    error_message: str = ""
    mock_seed: str = ""


@dataclass(frozen=True)
class ExternalManifestItem:
    external_asset_id: int
    origin_path_hash: str
    relative_path: str
    file_name: str
    mime_type: str
    file_size: int
    storage_key: str
    source_modified_at: float
    record_updated_at: float
    deleted: bool


@dataclass(frozen=True)
class ExternalManifestResult:
    items: list[ExternalManifestItem] = field(default_factory=list)
    manifest_id: str = ""
    etag: str = ""
    generated_at: float = 0.0
    active_count: int = 0
    deleted_count: int = 0
    not_modified: bool = False


@dataclass(frozen=True)
class ExternalDownloadTicket:
    external_asset_id: int
    status: str
    origin_path_hash: str = ""
    relative_path: str = ""
    file_name: str = ""
    storage_key: str = ""
    expected_size: int | None = None
    actual_size: int | None = None
    etag: str = ""
    crc64_ecma: str = ""
    whole_hash: str = ""
    download_url: str = ""
    expires_at: float = 0.0
    retryable: bool = False
    error_message: str = ""
    mock_seed: str = ""


@dataclass(frozen=True)
class ExternalSyncHead:
    cursor: str
    observed_at: float = 0.0


@dataclass(frozen=True)
class ExternalChangesResult:
    cursor: str
    next_cursor: str
    has_more: bool
    items: list[ExternalManifestItem] = field(default_factory=list)
    generated_at: float = 0.0


class SyncProvider(Protocol):
    def get_manifest(self, etag: str = "") -> ManifestResult: ...

    def get_download_tickets(self, task_asset_ids: list[int]) -> list[DownloadTicket]: ...

    def get_external_manifest(self, etag: str = "") -> ExternalManifestResult: ...

    def get_external_sync_head(self) -> ExternalSyncHead: ...

    def get_external_changes(
        self, cursor: str, *, limit: int = 500, wait_seconds: int = 0
    ) -> ExternalChangesResult: ...

    def get_external_download_tickets(
        self, external_asset_ids: list[int]
    ) -> list[ExternalDownloadTicket]: ...


class MockProvider:
    """Local deterministic implementation of the production manifest/ticket contract."""

    def __init__(self, settings: Settings, sample_count: int = 24):
        self.settings = settings
        self.sample_count = sample_count
        self._seed_dir = settings.data_root / "tmp" / "mock_seed"
        self._seed_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_samples()
        self._manifest_id = hashlib.sha256(
            f"asset-hub-mock:{sample_count}".encode()
        ).hexdigest()

    @property
    def etag(self) -> str:
        return f'W/"{self._manifest_id}"'

    def _ensure_samples(self) -> None:
        for i in range(self.sample_count):
            task_asset_id = 9_000_000 + i
            name = f"HQT{10000+i}-SAMPLE.jpg"
            path = self._seed_dir / f"{task_asset_id}.bin"
            if path.exists():
                continue
            payload = (f"ASSET:{task_asset_id}:{name}\n".encode() * 64) + _random_bytes(
                240_000
            )
            path.write_bytes(payload)

    def get_manifest(self, etag: str = "") -> ManifestResult:
        if etag and _etag_manifest_id(etag) == self._manifest_id:
            return ManifestResult(
                manifest_id=self._manifest_id,
                etag=self.etag,
                object_count=self.sample_count,
                not_modified=True,
            )
        now = time.time()
        items: list[ManifestItem] = []
        for i in range(self.sample_count):
            task_asset_id = 9_000_000 + i
            name = f"HQT{10000+i}-SAMPLE.jpg"
            seed = self._seed_dir / f"{task_asset_id}.bin"
            items.append(
                ManifestItem(
                    task_asset_id=task_asset_id,
                    storage_key=f"mock/finalized/{task_asset_id}/{name}",
                    file_name=name,
                    original_filename=name,
                    file_size=seed.stat().st_size,
                    whole_hash="",
                    asset_updated_at=seed.stat().st_mtime,
                    format="jpg",
                    mime_type="image/jpeg",
                    group_id=8_000_000 + i,
                    revision_id=7_000_000 + i,
                    revision_mode="single",
                    finalized_at=now,
                    task_id=6_000_000 + i,
                    task_no=f"MOCK-{i:04d}",
                    scope_kind="sku",
                    sku_code=f"HQT{10000+i}",
                    product_name=f"样本物料{i}",
                    revision_item_id=5_000_000 + i,
                    sort_order=0,
                    item_name="最终成品图",
                    mock_seed=str(seed),
                )
            )
        return ManifestResult(
            items=items,
            manifest_id=self._manifest_id,
            etag=self.etag,
            generated_at=now,
            object_count=self.sample_count,
        )

    def get_download_tickets(self, task_asset_ids: list[int]) -> list[DownloadTicket]:
        results: list[DownloadTicket] = []
        for task_asset_id in dict.fromkeys(task_asset_ids):
            index = task_asset_id - 9_000_000
            if index < 0 or index >= self.sample_count:
                results.append(
                    DownloadTicket(task_asset_id=task_asset_id, status="not_current")
                )
                continue
            name = f"HQT{10000+index}-SAMPLE.jpg"
            seed = self._seed_dir / f"{task_asset_id}.bin"
            size = seed.stat().st_size
            results.append(
                DownloadTicket(
                    task_asset_id=task_asset_id,
                    status="ready",
                    storage_key=f"mock/finalized/{task_asset_id}/{name}",
                    file_name=name,
                    expected_size=size,
                    actual_size=size,
                    etag=hashlib.md5(str(task_asset_id).encode()).hexdigest(),
                    download_url=f"mock://{task_asset_id}",
                    expires_at=time.time() + 3600,
                    mock_seed=str(seed),
                )
            )
        return results

    def get_external_manifest(self, etag: str = "") -> ExternalManifestResult:
        manifest_id = hashlib.sha256(b"asset-hub-mock:external-current").hexdigest()
        response_etag = f'W/"{manifest_id}"'
        return ExternalManifestResult(
            manifest_id=manifest_id,
            etag=response_etag,
            not_modified=bool(etag and _etag_manifest_id(etag) == manifest_id),
        )

    def get_external_sync_head(self) -> ExternalSyncHead:
        return ExternalSyncHead(cursor="mock-external-head", observed_at=time.time())

    def get_external_changes(
        self, cursor: str, *, limit: int = 500, wait_seconds: int = 0
    ) -> ExternalChangesResult:
        return ExternalChangesResult(
            cursor=cursor or "mock-external-head",
            next_cursor=cursor or "mock-external-head",
            has_more=False,
        )

    def get_external_download_tickets(
        self, external_asset_ids: list[int]
    ) -> list[ExternalDownloadTicket]:
        return [
            ExternalDownloadTicket(external_asset_id=value, status="not_current")
            for value in dict.fromkeys(external_asset_ids)
        ]


def _random_bytes(size: int) -> bytes:
    import os

    return os.urandom(size)


class HttpProvider:
    def __init__(self, settings: Settings):
        self.settings = settings
        if not settings.http.base_url:
            raise ValueError("http.base_url 未配置")
        headers = {}
        if settings.http.token:
            headers["X-Asset-Sync-Token"] = settings.http.token
        self._client = httpx.Client(
            base_url=settings.http.base_url.rstrip("/"),
            timeout=settings.http.timeout_sec,
            headers=headers,
            follow_redirects=True,
        )

    def get_manifest(self, etag: str = "") -> ManifestResult:
        headers = {"If-None-Match": etag} if etag else {}
        response = self._client.get(MANIFEST_PATH, headers=headers)
        if response.status_code == 304:
            if not etag:
                raise ValueError("manifest returned 304 without a local ETag")
            return ManifestResult(
                manifest_id=_etag_manifest_id(etag),
                etag=response.headers.get("ETag") or etag,
                not_modified=True,
            )
        response.raise_for_status()
        raw = _response_data(response)
        if int(raw.get("schema_version") or 0) != 1:
            raise ValueError(f"unsupported manifest schema_version={raw.get('schema_version')!r}")
        manifest_id = str(raw.get("manifest_id") or "").strip()
        if not manifest_id:
            raise ValueError("manifest_id is required")
        response_etag = response.headers.get("ETag") or f'W/"{manifest_id}"'
        if _etag_manifest_id(response_etag) != manifest_id:
            raise ValueError("manifest ETag does not match manifest_id")

        groups = raw.get("groups") or []
        if int(raw.get("group_count") or 0) != len(groups):
            raise ValueError("manifest group_count does not match groups")
        items: list[ManifestItem] = []
        revision_items: set[int] = set()
        objects: dict[int, tuple[str, int, str, str]] = {}
        for group in groups:
            group_id = int(group.get("group_id") or 0)
            revision_id = int(group.get("revision_id") or 0)
            task_id = int(group.get("task_id") or 0)
            if group_id <= 0 or revision_id <= 0 or task_id <= 0:
                raise ValueError("manifest group IDs must be positive integers")
            group_items = group.get("items") or []
            for item in group_items:
                task_asset_id = int(item.get("task_asset_id") or 0)
                revision_item_id = int(item.get("revision_item_id") or 0)
                file_size = int(item.get("file_size") or 0)
                storage_key = str(item.get("storage_key") or "").strip()
                file_name = str(item.get("file_name") or "").strip()
                original_filename = str(item.get("original_filename") or "").strip()
                whole_hash = str(item.get("whole_hash") or "").strip()
                if task_asset_id <= 0 or revision_item_id <= 0:
                    raise ValueError("manifest IDs must be positive integers")
                if revision_item_id in revision_items:
                    raise ValueError(f"duplicate revision_item_id={revision_item_id}")
                if file_size < 0 or not storage_key or not file_name:
                    raise ValueError(f"invalid manifest object task_asset_id={task_asset_id}")
                identity = (storage_key, file_size, file_name, whole_hash)
                previous = objects.setdefault(task_asset_id, identity)
                if previous != identity:
                    raise ValueError(
                        f"conflicting object metadata task_asset_id={task_asset_id}"
                    )
                revision_items.add(revision_item_id)
                items.append(
                    ManifestItem(
                        task_asset_id=task_asset_id,
                        storage_key=storage_key,
                        file_name=file_name,
                        original_filename=original_filename,
                        file_size=file_size,
                        whole_hash=whole_hash,
                        asset_updated_at=_timestamp(item.get("asset_updated_at")),
                        format=str(item.get("format") or ""),
                        mime_type=str(item.get("mime_type") or ""),
                        group_id=group_id,
                        revision_id=revision_id,
                        revision_mode=str(group.get("revision_mode") or ""),
                        finalized_at=_timestamp(group.get("finalized_at")),
                        task_id=task_id,
                        task_no=str(group.get("task_no") or ""),
                        scope_kind=str(group.get("scope_kind") or ""),
                        sku_code=str(group.get("sku_code") or ""),
                        product_name=str(group.get("product_name") or ""),
                        revision_item_id=revision_item_id,
                        sort_order=int(item.get("sort_order") or 0),
                        item_name=str(item.get("item_name") or ""),
                    )
                )
        if int(raw.get("item_count") or 0) != len(items):
            raise ValueError("manifest item_count does not match items")
        if int(raw.get("object_count") or 0) != len(objects):
            raise ValueError("manifest object_count does not match distinct task_asset_ids")
        expected_bytes = sum(identity[1] for identity in objects.values())
        if int(raw.get("total_object_bytes") or 0) != expected_bytes:
            raise ValueError("manifest total_object_bytes does not match objects")
        return ManifestResult(
            items=items,
            manifest_id=manifest_id,
            etag=response_etag,
            generated_at=_timestamp(raw.get("generated_at")),
            object_count=len(objects),
        )

    def get_download_tickets(self, task_asset_ids: list[int]) -> list[DownloadTicket]:
        ids = list(dict.fromkeys(task_asset_ids))
        if not 1 <= len(ids) <= 50:
            raise ValueError("task_asset_ids must contain between 1 and 50 items")
        response = self._client.post(TICKETS_PATH, json={"task_asset_ids": ids})
        response.raise_for_status()
        raw = _response_data(response)
        results: list[DownloadTicket] = []
        seen: set[int] = set()
        for item in raw.get("results") or []:
            task_asset_id = int(item.get("task_asset_id") or 0)
            if task_asset_id in seen:
                raise ValueError(f"duplicate ticket task_asset_id={task_asset_id}")
            seen.add(task_asset_id)
            status = str(item.get("status") or "")
            if status not in {"ready", "missing", "size_mismatch", "not_current", "error"}:
                raise ValueError(f"unknown ticket status={status!r}")
            results.append(
                DownloadTicket(
                    task_asset_id=task_asset_id,
                    status=status,
                    storage_key=str(item.get("storage_key") or ""),
                    file_name=str(item.get("file_name") or ""),
                    expected_size=_optional_int(item.get("expected_size")),
                    actual_size=_optional_int(item.get("actual_size")),
                    etag=str(item.get("etag") or ""),
                    crc64_ecma=str(item.get("crc64_ecma") or ""),
                    whole_hash=str(item.get("whole_hash") or ""),
                    download_url=str(item.get("download_url") or ""),
                    expires_at=_timestamp(item.get("expires_at")),
                    retryable=bool(item.get("retryable")),
                    error_message=str(item.get("error_message") or ""),
                )
            )
        return results

    def get_external_manifest(self, etag: str = "") -> ExternalManifestResult:
        headers = {"If-None-Match": etag} if etag else {}
        response = self._client.get(EXTERNAL_MANIFEST_PATH, headers=headers)
        if response.status_code == 304:
            if not etag:
                raise ValueError("external manifest returned 304 without a local ETag")
            return ExternalManifestResult(
                manifest_id=_etag_manifest_id(etag),
                etag=response.headers.get("ETag") or etag,
                not_modified=True,
            )
        response.raise_for_status()
        raw = _response_data(response)
        if int(raw.get("schema_version") or 0) != 1:
            raise ValueError(
                f"unsupported external manifest schema_version={raw.get('schema_version')!r}"
            )
        manifest_id = str(raw.get("manifest_id") or "").strip()
        if not manifest_id:
            raise ValueError("external manifest_id is required")
        response_etag = response.headers.get("ETag") or f'W/"{manifest_id}"'
        if _etag_manifest_id(response_etag) != manifest_id:
            raise ValueError("external manifest ETag does not match manifest_id")
        items: list[ExternalManifestItem] = []
        ids: set[int] = set()
        paths: set[str] = set()
        active_count = 0
        deleted_count = 0
        total_bytes = 0
        for raw_item in raw.get("items") or []:
            external_asset_id = int(raw_item.get("external_asset_id") or 0)
            relative_path = str(raw_item.get("relative_path") or "").strip()
            file_name = str(raw_item.get("file_name") or "").strip()
            file_size = int(raw_item.get("file_size") or 0)
            storage_key = str(raw_item.get("storage_key") or "").strip()
            deleted = bool(raw_item.get("deleted"))
            path_value = PurePosixPath(relative_path)
            if (
                external_asset_id <= 0
                or not relative_path
                or path_value.is_absolute()
                or ".." in path_value.parts
                or not file_name
                or file_size < 0
            ):
                raise ValueError(f"invalid external manifest item={external_asset_id}")
            if external_asset_id in ids or relative_path in paths:
                raise ValueError("external manifest IDs and relative paths must be unique")
            if deleted and storage_key:
                raise ValueError("deleted external manifest item carries storage_key")
            if not deleted and not storage_key:
                raise ValueError("active external manifest item is missing storage_key")
            ids.add(external_asset_id)
            paths.add(relative_path)
            if deleted:
                deleted_count += 1
            else:
                active_count += 1
                total_bytes += file_size
            items.append(
                ExternalManifestItem(
                    external_asset_id=external_asset_id,
                    origin_path_hash=str(raw_item.get("origin_path_hash") or ""),
                    relative_path=relative_path,
                    file_name=file_name,
                    mime_type=str(raw_item.get("mime_type") or ""),
                    file_size=file_size,
                    storage_key=storage_key,
                    source_modified_at=_timestamp(raw_item.get("source_modified_at")),
                    record_updated_at=_timestamp(raw_item.get("record_updated_at")),
                    deleted=deleted,
                )
            )
        if int(raw.get("item_count") or 0) != len(items):
            raise ValueError("external manifest item_count does not match items")
        if int(raw.get("active_count") or 0) != active_count:
            raise ValueError("external manifest active_count does not match items")
        if int(raw.get("deleted_count") or 0) != deleted_count:
            raise ValueError("external manifest deleted_count does not match items")
        if int(raw.get("total_object_bytes") or 0) != total_bytes:
            raise ValueError("external manifest total_object_bytes does not match items")
        return ExternalManifestResult(
            items=items,
            manifest_id=manifest_id,
            etag=response_etag,
            generated_at=_timestamp(raw.get("generated_at")),
            active_count=active_count,
            deleted_count=deleted_count,
        )

    def get_external_sync_head(self) -> ExternalSyncHead:
        response = self._client.get(EXTERNAL_HEAD_PATH)
        response.raise_for_status()
        raw = _response_data(response)
        if int(raw.get("schema_version") or 0) != 1:
            raise ValueError("unsupported external head schema_version")
        cursor = str(raw.get("cursor") or "").strip()
        if not cursor:
            raise ValueError("external head cursor is required")
        return ExternalSyncHead(
            cursor=cursor,
            observed_at=_timestamp(raw.get("observed_at")),
        )

    def get_external_changes(
        self, cursor: str, *, limit: int = 500, wait_seconds: int = 0
    ) -> ExternalChangesResult:
        if not 1 <= limit <= 500:
            raise ValueError("external changes limit must be between 1 and 500")
        if not 0 <= wait_seconds <= 30:
            raise ValueError("external changes wait_seconds must be between 0 and 30")
        response = self._client.get(
            EXTERNAL_CHANGES_PATH,
            params={"cursor": cursor, "limit": limit, "wait_seconds": wait_seconds},
            timeout=max(self.settings.http.timeout_sec, wait_seconds + 10),
        )
        response.raise_for_status()
        raw = _response_data(response)
        if int(raw.get("schema_version") or 0) != 1:
            raise ValueError("unsupported external changes schema_version")
        response_cursor = str(raw.get("cursor") or "").strip()
        next_cursor = str(raw.get("next_cursor") or "").strip()
        if not response_cursor or not next_cursor:
            raise ValueError("external changes cursors are required")
        if cursor and response_cursor != cursor:
            raise ValueError("external changes response cursor differs from request")
        items, _active, _deleted, _bytes = _parse_external_items(raw.get("items") or [])
        if items and next_cursor == response_cursor:
            raise ValueError("external changes did not advance for a non-empty batch")
        return ExternalChangesResult(
            cursor=response_cursor,
            next_cursor=next_cursor,
            has_more=bool(raw.get("has_more")),
            items=items,
            generated_at=_timestamp(raw.get("generated_at")),
        )

    def get_external_download_tickets(
        self, external_asset_ids: list[int]
    ) -> list[ExternalDownloadTicket]:
        ids = list(dict.fromkeys(external_asset_ids))
        if not 1 <= len(ids) <= 50:
            raise ValueError("external_asset_ids must contain between 1 and 50 items")
        response = self._client.post(
            EXTERNAL_TICKETS_PATH, json={"external_asset_ids": ids}
        )
        response.raise_for_status()
        raw = _response_data(response)
        results: list[ExternalDownloadTicket] = []
        seen: set[int] = set()
        for item in raw.get("results") or []:
            external_asset_id = int(item.get("external_asset_id") or 0)
            if external_asset_id in seen:
                raise ValueError(
                    f"duplicate external ticket external_asset_id={external_asset_id}"
                )
            seen.add(external_asset_id)
            status = str(item.get("status") or "")
            if status not in {"ready", "missing", "size_mismatch", "not_current", "error"}:
                raise ValueError(f"unknown external ticket status={status!r}")
            results.append(
                ExternalDownloadTicket(
                    external_asset_id=external_asset_id,
                    status=status,
                    origin_path_hash=str(item.get("origin_path_hash") or ""),
                    relative_path=str(item.get("relative_path") or ""),
                    file_name=str(item.get("file_name") or ""),
                    storage_key=str(item.get("storage_key") or ""),
                    expected_size=_optional_int(item.get("expected_size")),
                    actual_size=_optional_int(item.get("actual_size")),
                    etag=str(item.get("etag") or ""),
                    crc64_ecma=str(item.get("crc64_ecma") or ""),
                    download_url=str(item.get("download_url") or ""),
                    expires_at=_timestamp(item.get("expires_at")),
                    retryable=bool(item.get("retryable")),
                    error_message=str(item.get("error_message") or ""),
                )
            )
        return results


def _response_data(response: httpx.Response) -> dict:
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ValueError("expected JSON response envelope with data object")
    return payload["data"]


def _parse_external_items(
    raw_items: list[dict],
) -> tuple[list[ExternalManifestItem], int, int, int]:
    items: list[ExternalManifestItem] = []
    ids: set[int] = set()
    paths: set[str] = set()
    active_count = deleted_count = total_bytes = 0
    for raw_item in raw_items:
        external_asset_id = int(raw_item.get("external_asset_id") or 0)
        relative_path = str(raw_item.get("relative_path") or "").strip()
        file_name = str(raw_item.get("file_name") or "").strip()
        file_size = int(raw_item.get("file_size") or 0)
        storage_key = str(raw_item.get("storage_key") or "").strip()
        deleted = bool(raw_item.get("deleted"))
        path_value = PurePosixPath(relative_path)
        if (
            external_asset_id <= 0
            or not relative_path
            or path_value.is_absolute()
            or ".." in path_value.parts
            or not file_name
            or file_size < 0
        ):
            raise ValueError(f"invalid external manifest item={external_asset_id}")
        if external_asset_id in ids or relative_path in paths:
            raise ValueError("external manifest IDs and relative paths must be unique")
        if deleted and storage_key:
            raise ValueError("deleted external manifest item carries storage_key")
        if not deleted and not storage_key:
            raise ValueError("active external manifest item is missing storage_key")
        ids.add(external_asset_id)
        paths.add(relative_path)
        if deleted:
            deleted_count += 1
        else:
            active_count += 1
            total_bytes += file_size
        items.append(
            ExternalManifestItem(
                external_asset_id=external_asset_id,
                origin_path_hash=str(raw_item.get("origin_path_hash") or ""),
                relative_path=relative_path,
                file_name=file_name,
                mime_type=str(raw_item.get("mime_type") or ""),
                file_size=file_size,
                storage_key=storage_key,
                source_modified_at=_timestamp(raw_item.get("source_modified_at")),
                record_updated_at=_timestamp(raw_item.get("record_updated_at")),
                deleted=deleted,
            )
        )
    return items, active_count, deleted_count, total_bytes


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def copy_mock_ticket(ticket: DownloadTicket, destination: Path) -> bool:
    if not ticket.mock_seed:
        return False
    seed = Path(ticket.mock_seed)
    if not seed.is_file():
        raise FileNotFoundError(f"mock seed missing: {ticket.task_asset_id}")
    shutil.copyfile(seed, destination)
    return True


def build_provider(settings: Settings) -> SyncProvider:
    if settings.provider == "http":
        return HttpProvider(settings)
    if settings.provider == "mock":
        return MockProvider(settings)
    raise ValueError(f"unsupported provider={settings.provider!r}")
