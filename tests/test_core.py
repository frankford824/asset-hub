from __future__ import annotations

import time
from pathlib import Path

import openpyxl
import pytest
from fastapi.testclient import TestClient

from asset_hub.catalog.db import AssetRow, Catalog
from asset_hub.catalog.ignore import should_ignore
from asset_hub.catalog.index import index_library
from asset_hub.config import Settings, ensure_data_dirs, get_settings
from asset_hub.jobs import JobStore
from asset_hub.pack.excel import match_assets_for_rows, read_excel_rows
from asset_hub.pack.ziputil import zip_paths
from asset_hub.sync.main import sync_once


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    data = tmp_path / "data"
    lib = tmp_path / "library"
    lib.mkdir()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"""
library_root: {lib}
data_root: {data}
local_only: true
provider: mock
api:
  host: 127.0.0.1
  port: 18080
sync:
  kinds: [finalized]
  ignore_globs: ["Thumbs.db", "desktop.ini", "._*"]
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("ASSET_HUB_CONFIG", str(cfg))
    monkeypatch.delenv("ASSET_HUB_DATA", raising=False)
    monkeypatch.delenv("ASSET_HUB_LIBRARY", raising=False)
    monkeypatch.setenv("ASSET_HUB_X_ACCEL", "0")
    get_settings.cache_clear()
    s = ensure_data_dirs(get_settings())
    yield s
    get_settings.cache_clear()


def test_ignore_rules():
    assert should_ignore("Thumbs.db")
    assert should_ignore("desktop.ini")
    assert should_ignore("._hidden")
    assert not should_ignore("photo.jpg")


def test_catalog_search_sku(settings):
    cat = Catalog(settings)
    cat.upsert_asset(
        AssetRow(
            asset_id="a1",
            kind="finalized",
            file_name="HQT10001-x.jpg",
            sku_code="HQT10001",
            sku_name="测试品",
            local_path="/tmp/x",
            status="ready",
        )
    )
    hits = cat.search("HQT10001", kind="finalized")
    assert len(hits[0]) == 1
    assert hits[0][0].asset_id == "a1"


def test_sync_mock_and_ready(settings):
    stats = sync_once()
    assert stats["written"] > 0 or stats["fetched"] > 0
    cat = Catalog(settings)
    assert cat.count_ready("finalized") > 0
    assert cat.is_finalized_ready()


def test_index_library(settings):
    root = settings.library_root
    (root / "skuA").mkdir()
    (root / "skuA" / "a.jpg").write_bytes(b"abc")
    (root / "Thumbs.db").write_bytes(b"x")
    n = index_library(Catalog(settings), root, settings.sync.ignore_globs)
    assert n == 1
    hits = Catalog(settings).search("a.jpg", kind="library")
    assert len(hits[0]) == 1


def test_zip_store_for_jpg(settings, tmp_path):
    src = tmp_path / "x.jpg"
    src.write_bytes(b"\xff\xd8" + b"0" * 100)
    out = settings.tmp_dir / "t.zip"
    zip_paths([(src, "pack/x.jpg")], out)
    assert out.is_file()
    import zipfile

    with zipfile.ZipFile(out) as zf:
        info = zf.getinfo("pack/x.jpg")
        assert info.compress_type == zipfile.ZIP_STORED


def test_excel_match_and_job(settings):
    sync_once()
    cat = Catalog(settings)
    xlsx = settings.tmp_dir / "pack.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["SKU编码", "名称"])
    ws.append(["HQT10000", "样本"])
    ws.append(["MISSING999", "无"])
    wb.save(xlsx)
    rows = read_excel_rows(xlsx)
    matched, missing = match_assets_for_rows(cat, rows)
    assert matched
    assert missing

    store = JobStore(settings)
    job = store.create(filename="pack.xlsx", super_dir_name="demo")
    job_dir = store.job_dir(job.id)
    (job_dir / "input.xlsx").write_bytes(xlsx.read_bytes())
    claimed = store.claim_next()
    assert claimed and claimed.status == "running"

    from asset_hub.worker.main import process_job

    process_job(store, cat, claimed.id)
    done = store.get(claimed.id)
    assert done.status == "done"
    assert Path(done.archive_path).is_file()


def test_api_health_search(settings):
    from asset_hub.api.main import app

    sync_once()
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    r = client.get("/api/v1/search", params={"q": "HQT10000"})
    assert r.status_code == 200
    assert r.json()["results"]
