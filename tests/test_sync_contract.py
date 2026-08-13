from __future__ import annotations

import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from asset_hub.catalog.db import Catalog
from asset_hub.config import ensure_data_dirs, get_settings
from asset_hub.sync.main import sync_once


def _manifest(manifest_id: str, object_ids: list[int]) -> dict:
    groups = []
    total_bytes = 0
    for index, task_asset_id in enumerate(object_ids):
        size = len(f"object-{task_asset_id}".encode())
        total_bytes += size
        groups.append(
            {
                "group_id": 1000 + index,
                "revision_id": 2000 + index,
                "revision_mode": "single",
                "finalized_at": "2026-08-12T10:00:00Z",
                "task_id": 3000 + index,
                "task_no": f"RW-{index}",
                "scope_kind": "sku",
                "sku_code": f"SKU{task_asset_id}",
                "product_name": f"Product {task_asset_id}",
                "items": [
                    {
                        "revision_item_id": 4000 + index,
                        "sort_order": index,
                        "item_name": "最终成品图",
                        "task_asset_id": task_asset_id,
                        "file_name": f"SKU{task_asset_id}.jpg",
                        "original_filename": f"SKU{task_asset_id}.jpg",
                        "format": "jpg",
                        "mime_type": "image/jpeg",
                        "file_size": size,
                        "storage_key": f"tasks/{task_asset_id}.jpg",
                        "whole_hash": None,
                        "asset_updated_at": "2026-08-12T10:00:00Z",
                    }
                ],
            }
        )
    return {
        "schema_version": 1,
        "manifest_id": manifest_id,
        "generated_at": "2026-08-12T10:01:00Z",
        "group_count": len(groups),
        "item_count": len(groups),
        "object_count": len(object_ids),
        "total_object_bytes": total_bytes,
        "groups": groups,
    }


class ContractStub:
    def __init__(self):
        self.manifest = _manifest("manifest-v1", [501, 502])
        self.etag = 'W/"manifest-v1"'
        self.ticket_status: dict[int, dict] = {}
        self.requests: list[tuple[str, str, str]] = []
        self.ticket_batches: list[list[int]] = []
        self.server: ThreadingHTTPServer | None = None

    def handler(self):
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

            def _json(self, status: int, payload: dict, *, etag: str = ""):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                if etag:
                    self.send_header("ETag", etag)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                token = self.headers.get("X-Asset-Sync-Token", "")
                state.requests.append(
                    (self.command, self.path, self.headers.get("If-None-Match", ""))
                )
                if self.path.endswith("/finalized/manifest"):
                    if token != "contract-secret":
                        self._json(401, {"error": "unauthorized"})
                        return
                    if self.headers.get("If-None-Match") == state.etag:
                        self.send_response(304)
                        self.send_header("ETag", state.etag)
                        self.end_headers()
                        return
                    self._json(200, {"data": state.manifest}, etag=state.etag)
                    return
                if self.path.startswith("/objects/"):
                    task_asset_id = int(self.path.rsplit("/", 1)[-1])
                    body = f"object-{task_asset_id}".encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self._json(404, {"error": "not found"})

            def do_POST(self):
                token = self.headers.get("X-Asset-Sync-Token", "")
                state.requests.append((self.command, self.path, ""))
                if token != "contract-secret":
                    self._json(401, {"error": "unauthorized"})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                ids = payload.get("task_asset_ids") or []
                state.ticket_batches.append(ids)
                results = []
                assert state.server is not None
                base_url = f"http://127.0.0.1:{state.server.server_port}"
                for task_asset_id in ids:
                    custom = state.ticket_status.get(task_asset_id)
                    if custom is not None:
                        results.append({"task_asset_id": task_asset_id, **custom})
                        continue
                    size = len(f"object-{task_asset_id}".encode())
                    results.append(
                        {
                            "task_asset_id": task_asset_id,
                            "status": "ready",
                            "storage_key": f"tasks/{task_asset_id}.jpg",
                            "file_name": f"SKU{task_asset_id}.jpg",
                            "expected_size": size,
                            "actual_size": size,
                            "etag": f"etag-{task_asset_id}",
                            "crc64_ecma": f"crc-{task_asset_id}",
                            "whole_hash": None,
                            "download_url": f"{base_url}/objects/{task_asset_id}",
                            "expires_at": "2030-08-12T10:15:00Z",
                            "retryable": False,
                        }
                    )
                self._json(200, {"data": {"results": results}})

        return Handler

    def __enter__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_args):
        assert self.server is not None
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture()
def http_settings(tmp_path, monkeypatch):
    def configure(base_url: str):
        data = tmp_path / "data"
        library = tmp_path / "library"
        library.mkdir(exist_ok=True)
        config = tmp_path / "config.yaml"
        config.write_text(
            f"""
library_root: {library}
data_root: {data}
local_only: true
provider: http
http:
  base_url: {base_url}
  token: contract-secret
  timeout_sec: 5
sync:
  kinds: [finalized]
  ticket_batch_size: 50
  verify_interval_sec: 86400
  ignore_globs: ["Thumbs.db", "desktop.ini", "._*"]
""".strip(),
            encoding="utf-8",
        )
        monkeypatch.setenv("ASSET_HUB_CONFIG", str(config))
        monkeypatch.delenv("ASSET_HUB_DATA", raising=False)
        monkeypatch.delenv("ASSET_HUB_LIBRARY", raising=False)
        get_settings.cache_clear()
        return ensure_data_dirs(get_settings())

    yield configure
    get_settings.cache_clear()


def test_http_manifest_ticket_download_and_tombstone(http_settings):
    with ContractStub() as stub:
        settings = http_settings(f"http://127.0.0.1:{stub.server.server_port}")
        first = sync_once()
        assert first["ready_for_pack"] is True
        assert first["written"] == 2
        assert stub.ticket_batches == [[501, 502]]

        catalog = Catalog(settings)
        asset = catalog.get_asset_by_task_asset_id(501)
        assert asset is not None
        assert asset.etag == "etag-501"
        assert asset.crc64_ecma == "crc-501"
        assert Path(asset.local_path).read_bytes() == b"object-501"
        assert not Path(asset.local_path + ".part").exists()
        item = catalog.list_finalized_items()[0]
        assert item["group_id"] == 1000
        assert item["revision_id"] == 2000
        assert item["sort_order"] == 0

        second = sync_once()
        assert second["ready_for_pack"] is True
        assert second["requested"] == 0
        assert stub.ticket_batches == [[501, 502]]
        assert any(request[2] == 'W/"manifest-v1"' for request in stub.requests)

        removed_path = Path(catalog.get_asset_by_task_asset_id(502).local_path)
        stub.manifest = _manifest("manifest-v2", [501])
        stub.manifest["groups"][0]["items"][0]["asset_updated_at"] = (
            "2026-08-12T11:00:00Z"
        )
        stub.etag = 'W/"manifest-v2"'
        third = sync_once()
        assert third["ready_for_pack"] is True
        assert third["tombstone"] == 1
        assert third["requested"] == 1
        exited = catalog.get_asset_by_task_asset_id(502)
        assert exited is not None and exited.status == "tombstone" and exited.deleted == 1
        assert removed_path.is_file(), "snapshot exits must not delete cached files"
        deleted_items = [
            row
            for row in catalog.list_finalized_items(include_deleted=True)
            if row["task_asset_id"] == 502
        ]
        assert deleted_items and deleted_items[0]["deleted"] == 1


def test_retryable_ticket_error_keeps_sync_not_ready_then_retries_304(http_settings):
    with ContractStub() as stub:
        settings = http_settings(f"http://127.0.0.1:{stub.server.server_port}")
        stub.ticket_status[502] = {
            "status": "error",
            "retryable": True,
            "error_message": "temporary OSS error",
        }
        first = sync_once()
        assert first["ready_for_pack"] is False
        assert first["retryable_error"] == 1
        state = Catalog(settings).get_sync_state("finalized")
        assert state["ready"] == 0
        failed = Catalog(settings).get_asset_by_task_asset_id(502)
        assert failed is not None and failed.retryable == 1 and failed.status == "error"

        stub.ticket_status.pop(502)
        second = sync_once()
        assert second["ready_for_pack"] is True
        assert second["requested"] == 1
        assert stub.ticket_batches[-1] == [502]
        recovered = Catalog(settings).get_asset_by_task_asset_id(502)
        assert recovered is not None and recovered.status == "ready"


def test_nonretryable_ticket_error_waits_for_full_verification(http_settings):
    with ContractStub() as stub:
        settings = http_settings(f"http://127.0.0.1:{stub.server.server_port}")
        stub.ticket_status[502] = {
            "status": "error",
            "retryable": False,
            "error_message": "object configuration is invalid",
        }
        first = sync_once()
        assert first["ready_for_pack"] is False
        assert first["retryable_error"] == 0

        second = sync_once()
        assert second["ready_for_pack"] is False
        assert second["requested"] == 0

        stub.ticket_status.pop(502)
        catalog = Catalog(settings)
        with catalog.connect() as connection:
            connection.execute(
                "UPDATE sync_state SET last_verified_at=0 WHERE kind='finalized'"
            )
        third = sync_once()
        assert third["ready_for_pack"] is True
        assert third["requested"] == 2


def test_catalog_additively_migrates_pre_manifest_database(tmp_path):
    from asset_hub.config import Settings

    settings = Settings(data_root=tmp_path / "data", library_root=tmp_path / "library")
    settings.db_path.parent.mkdir(parents=True)
    with sqlite3.connect(settings.db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE assets (
              asset_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
              storage_key TEXT NOT NULL DEFAULT '', file_name TEXT NOT NULL DEFAULT '',
              original_filename TEXT NOT NULL DEFAULT '', file_size INTEGER NOT NULL DEFAULT 0,
              etag TEXT NOT NULL DEFAULT '', whole_hash TEXT NOT NULL DEFAULT '',
              sku_code TEXT NOT NULL DEFAULT '', sku_name TEXT NOT NULL DEFAULT '',
              local_path TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'ready',
              updated_at REAL NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE sync_state (
              kind TEXT PRIMARY KEY, cursor TEXT NOT NULL DEFAULT '',
              last_success_at REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
              ready INTEGER NOT NULL DEFAULT 0, stats_json TEXT NOT NULL DEFAULT '{}'
            );
            """
        )
    catalog = Catalog(settings)
    with catalog.connect() as connection:
        asset_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(assets)")
        }
        sync_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(sync_state)")
        }
    assert {"task_asset_id", "crc64_ecma", "retryable", "manifest_id"} <= asset_columns
    assert {"etag", "manifest_id", "last_verified_at"} <= sync_columns
