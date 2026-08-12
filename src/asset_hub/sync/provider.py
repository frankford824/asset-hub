from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import httpx

from asset_hub.config import Settings


@dataclass
class SyncItem:
    asset_id: str
    storage_key: str
    file_name: str
    file_size: int
    updated_at: float
    deleted: bool = False
    original_filename: str = ""
    etag: str = ""
    whole_hash: str = ""
    sku_code: str = ""
    sku_name: str = ""
    kind: str = "finalized"
    # mock-only: optional seed content path or inline bytes marker
    mock_seed: str = ""


@dataclass
class ListResult:
    items: list[SyncItem] = field(default_factory=list)
    next_cursor: str = ""


@dataclass
class DownloadInfo:
    url: str
    expires_at: float = 0


class SyncProvider(Protocol):
    def list_items(self, kind: str, cursor: str, limit: int = 200) -> ListResult: ...

    def resolve_download(self, storage_key: str) -> DownloadInfo: ...


class MockProvider:
    """本地假清单：在 NVMe finalized 写入真实样本文件，供热路径压测。"""

    def __init__(self, settings: Settings, sample_count: int = 24):
        self.settings = settings
        self.sample_count = sample_count
        self._seed_dir = settings.data_root / "tmp" / "mock_seed"
        self._seed_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_samples()

    def _ensure_samples(self) -> None:
        for i in range(self.sample_count):
            asset_id = f"mock-{i:04d}"
            name = f"HQT{10000+i}-SAMPLE.jpg"
            path = self._seed_dir / f"{asset_id}.bin"
            if path.exists():
                continue
            # ~256KB compressible-ish but we zip with STORE anyway
            payload = (f"ASSET:{asset_id}:{name}\n".encode() * 64) + os_urandom(240_000)
            path.write_bytes(payload)

    def list_items(self, kind: str, cursor: str, limit: int = 200) -> ListResult:
        if kind not in ("finalized", "archive", "xukai_current"):
            return ListResult()
        start = int(cursor) if cursor.isdigit() else 0
        items: list[SyncItem] = []
        end = min(start + limit, self.sample_count)
        now = time.time()
        for i in range(start, end):
            asset_id = f"mock-{i:04d}"
            name = f"HQT{10000+i}-SAMPLE.jpg"
            seed = self._seed_dir / f"{asset_id}.bin"
            size = seed.stat().st_size if seed.exists() else 0
            items.append(
                SyncItem(
                    asset_id=asset_id,
                    storage_key=f"mock/finalized/{asset_id}/{name}",
                    file_name=name,
                    original_filename=name,
                    file_size=size,
                    updated_at=now,
                    sku_code=f"HQT{10000+i}",
                    sku_name=f"样本物料{i}",
                    kind="finalized" if kind != "archive" else "archive",
                    mock_seed=str(seed),
                    etag=hashlib.md5(asset_id.encode()).hexdigest(),
                )
            )
        next_cursor = str(end) if end < self.sample_count else ""
        return ListResult(items=items, next_cursor=next_cursor)

    def resolve_download(self, storage_key: str) -> DownloadInfo:
        # file:// style handled by sync runner via mock_seed; URL is placeholder
        return DownloadInfo(url=f"mock://{storage_key}", expires_at=time.time() + 3600)


def os_urandom(n: int) -> bytes:
    import os

    return os.urandom(n)


class HttpProvider:
    def __init__(self, settings: Settings):
        self.settings = settings
        if not settings.http.base_url:
            raise ValueError("http.base_url 未配置")
        self._client = httpx.Client(
            base_url=settings.http.base_url.rstrip("/"),
            timeout=settings.http.timeout_sec,
            headers={"Authorization": f"Bearer {settings.http.token}"}
            if settings.http.token
            else {},
        )

    def list_items(self, kind: str, cursor: str, limit: int = 200) -> ListResult:
        r = self._client.get(
            "/sync/list",
            params={"kind": kind, "cursor": cursor, "limit": limit},
        )
        r.raise_for_status()
        data = r.json()
        items = []
        for raw in data.get("items") or []:
            items.append(
                SyncItem(
                    asset_id=str(raw["asset_id"]),
                    storage_key=str(raw.get("storage_key") or ""),
                    file_name=str(raw.get("file_name") or ""),
                    original_filename=str(raw.get("original_filename") or ""),
                    file_size=int(raw.get("file_size") or 0),
                    updated_at=float(raw.get("updated_at") or time.time()),
                    deleted=bool(raw.get("deleted")),
                    etag=str(raw.get("etag") or ""),
                    whole_hash=str(raw.get("whole_hash") or ""),
                    sku_code=str(raw.get("sku_code") or ""),
                    sku_name=str(raw.get("sku_name") or ""),
                    kind=str(raw.get("kind") or kind),
                )
            )
        return ListResult(items=items, next_cursor=str(data.get("next_cursor") or ""))

    def resolve_download(self, storage_key: str) -> DownloadInfo:
        r = self._client.get(
            "/sync/download",
            params={"storage_key": storage_key},
        )
        r.raise_for_status()
        data = r.json()
        return DownloadInfo(
            url=str(data["url"]),
            expires_at=float(data.get("expires_at") or 0),
        )


def build_provider(settings: Settings) -> SyncProvider:
    if settings.provider == "http":
        return HttpProvider(settings)
    return MockProvider(settings)
