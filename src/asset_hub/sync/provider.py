from __future__ import annotations

import hashlib
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

import httpx

from asset_hub.config import Settings


MANIFEST_PATH = "/v1/integration/asset-sync/finalized/manifest"
TICKETS_PATH = "/v1/integration/asset-sync/finalized/download-tickets"


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


class SyncProvider(Protocol):
    def get_manifest(self, etag: str = "") -> ManifestResult: ...

    def get_download_tickets(self, task_asset_ids: list[int]) -> list[DownloadTicket]: ...


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


def _response_data(response: httpx.Response) -> dict:
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ValueError("expected JSON response envelope with data object")
    return payload["data"]


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
