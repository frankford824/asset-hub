from __future__ import annotations

import time
from io import BytesIO
from pathlib import Path

import openpyxl
import pytest
from fastapi.testclient import TestClient

from asset_hub.catalog.db import AssetRow, Catalog
from asset_hub.catalog.ignore import should_ignore
from asset_hub.catalog.index import index_library
from asset_hub.config import Settings, ensure_data_dirs, get_settings
from asset_hub.jobs import JobStore
from asset_hub.pack.excel import ExcelRow, match_assets_for_rows, read_excel_rows
from asset_hub.pack.ziputil import zip_paths
from asset_hub.pack.rules import PackRuleStore
from asset_hub.sync.provider import ManifestItem
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


def test_catalog_bulk_upsert(settings):
    cat = Catalog(settings)
    rows = [
        AssetRow(
            asset_id=f"bulk:{index}",
            kind="library",
            file_name=f"HQT-BULK-{index}.jpg",
            sku_code=f"HQT-BULK-{index}",
            status="ready",
        )
        for index in range(3)
    ]

    assert cat.upsert_assets(rows) == 3
    assert cat.count_ready("library") == 3


def test_unified_search_ready_only_and_current_first(settings, tmp_path):
    cat = Catalog(settings)
    library_file = tmp_path / "library-HQT20001.jpg"
    current_file = tmp_path / "current-HQT20001.jpg"
    library_file.write_bytes(b"library")
    current_file.write_bytes(b"current")
    cat.upsert_asset(
        AssetRow(
            asset_id="lib:HQT20001.jpg",
            kind="library",
            file_name=library_file.name,
            sku_code="HQT20001",
            local_path=str(library_file),
            status="ready",
        )
    )
    cat.upsert_asset(
        AssetRow(
            asset_id="finalized:20001",
            task_asset_id=20001,
            kind="finalized",
            file_name=current_file.name,
            sku_code="HQT20001",
            local_path=str(current_file),
            status="ready",
        )
    )
    cat.upsert_asset(
        AssetRow(
            asset_id="finalized:pending",
            task_asset_id=20002,
            kind="finalized",
            file_name="pending-HQT20001.jpg",
            sku_code="HQT20001",
            status="pending",
        )
    )

    hits, total = cat.search("HQT20001")
    assert total == 2
    assert [hit.asset_id for hit in hits] == [
        "finalized:20001",
        "lib:HQT20001.jpg",
    ]


def test_unified_match_prefers_current_and_falls_back(settings, tmp_path):
    cat = Catalog(settings)
    current = tmp_path / "current.jpg"
    fallback_same = tmp_path / "fallback-same.jpg"
    fallback_only = tmp_path / "fallback-only.jpg"
    current.write_bytes(b"current")
    fallback_same.write_bytes(b"fallback")
    fallback_only.write_bytes(b"fallback-only")
    for asset in (
        AssetRow(
            asset_id="finalized:30001",
            task_asset_id=30001,
            kind="finalized",
            file_name=current.name,
            sku_code="HQT30001",
            local_path=str(current),
            status="ready",
        ),
        AssetRow(
            asset_id="lib:HQT30001.jpg",
            kind="library",
            file_name=fallback_same.name,
            sku_code="HQT30001",
            local_path=str(fallback_same),
            status="ready",
        ),
        AssetRow(
            asset_id="lib:HQT30002.jpg",
            kind="library",
            file_name=fallback_only.name,
            sku_code="HQT30002",
            local_path=str(fallback_only),
            status="ready",
        ),
    ):
        cat.upsert_asset(asset)

    matched, missing = match_assets_for_rows(
        cat,
        [
            ExcelRow(row_index=2, sku_code="HQT30001"),
            ExcelRow(row_index=3, sku_code="HQT30002"),
        ],
    )
    assert not missing
    assert matched[0]["selection_policy"] == "preferred_current"
    assert [asset.asset_id for asset in matched[0]["assets"]] == ["finalized:30001"]
    assert matched[1]["selection_policy"] == "library_fallback"
    assert [asset.asset_id for asset in matched[1]["assets"]] == ["lib:HQT30002.jpg"]


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
    assert done.progress["preferred_rows"] >= 1
    import zipfile

    with zipfile.ZipFile(done.archive_path) as zf:
        report_names = [name for name in zf.namelist() if name.endswith("素材选择说明.txt")]
        assert report_names
        report = zf.read(report_names[0]).decode("utf-8")
        assert "当前优选" in report
        assert "MISSING999" in report


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
    result = r.json()["results"][0]
    assert "kind" not in result
    assert "local_path" not in result
    assert "status" not in result
    status = client.get("/api/v1/status").json()
    assert status["asset_count"] > 0
    assert status["ready_for_pack"] is True

    store = JobStore(settings)
    store.create(filename="timing.xlsx")
    claimed = store.claim_next()
    assert claimed and claimed.started_at
    jobs = client.get("/api/v1/jobs").json()["jobs"]
    assert jobs[0]["started_at"] == pytest.approx(claimed.started_at)


def test_create_job_allows_library_fallback_before_sync_complete(settings):
    cat = Catalog(settings)
    local = settings.library_root / "HQT40001.jpg"
    local.write_bytes(b"library")
    cat.upsert_asset(
        AssetRow(
            asset_id="lib:HQT40001.jpg",
            kind="library",
            file_name=local.name,
            sku_code="HQT40001",
            local_path=str(local),
            status="ready",
        )
    )
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["SKU编码", "名称"])
    ws.append(["HQT40001", "样本"])
    payload = BytesIO()
    wb.save(payload)

    from asset_hub.api.main import app

    client = TestClient(app)
    response = client.post(
        "/api/v1/jobs",
        files={
            "file": (
                "library-fallback.xlsx",
                payload.getvalue(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert response.status_code == 200
    assert response.json()["job_id"]


def test_library_upload_tree_and_global_filename_dedupe(settings):
    from asset_hub.api.main import app

    client = TestClient(app)
    response = client.post(
        "/api/v1/library/upload",
        data={"target_path": "产品图/主图", "relative_paths": '["manual.png"]'},
        files=[("files", ("manual.png", b"png-data", "image/png"))],
    )
    assert response.status_code == 200
    assert response.json()["added"] == 1

    tree = client.get("/api/v1/library/tree", params={"path": "产品图/主图"})
    assert tree.status_code == 200
    assert tree.json()["files"][0]["virtual_path"] == "产品图/主图/manual.png"

    duplicate = client.post(
        "/api/v1/library/upload",
        data={"target_path": "另一个目录", "relative_paths": '["manual.png"]'},
        files=[("files", ("manual.png", b"different", "image/png"))],
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "FILENAME_DUPLICATE"
    assert len(duplicate.json()["detail"]["duplicates"]) == 1
    assert not (settings.library_root / "另一个目录" / "manual.png").exists()


def test_manifest_reuses_existing_filename_without_download_candidate(settings):
    local = settings.library_root / "SAME-NAME.jpg"
    local.write_bytes(b"manual-content")
    catalog = Catalog(settings)
    catalog.upsert_asset(
        AssetRow(
            asset_id="upload:manual",
            kind="library",
            file_name=local.name,
            original_filename=local.name,
            file_size=local.stat().st_size,
            local_path=str(local),
            status="ready",
            virtual_path="手动/SAME-NAME.jpg",
        )
    )
    item = ManifestItem(
        task_asset_id=88001,
        storage_key="tasks/88/SAME-NAME.jpg",
        file_name="SAME-NAME.jpg",
        original_filename="SAME-NAME.jpg",
        file_size=999999,
        whole_hash="",
        asset_updated_at=time.time(),
        format="jpg",
        mime_type="image/jpeg",
        group_id=88,
        revision_id=89,
        revision_mode="single",
        finalized_at=time.time(),
        task_id=90,
        task_no="T-90",
        scope_kind="sku",
        sku_code="SAME88001",
        product_name="同名素材",
        revision_item_id=91,
        sort_order=0,
        item_name="主图",
    )
    catalog.apply_finalized_manifest([item], "manifest-dedupe")
    alias = catalog.get_asset_by_task_asset_id(88001)
    assert alias and alias.status == "ready"
    assert alias.local_path == str(local)
    assert alias.dedup_of_asset_id == "upload:manual"
    assert catalog.ticket_candidates(include_nonready=True) == []

    # If the original canonical row exits, the still-current alias must take
    # over the global filename claim instead of disappearing from the tree.
    catalog.mark_tombstone("upload:manual")
    promoted = catalog.find_asset_by_name("same-name.JPG")
    assert promoted and promoted.asset_id == alias.asset_id
    _folders, files, total = catalog.list_directory("SAME88001")
    assert total == 1
    assert files[0].asset_id == alias.asset_id
    duplicate = catalog.reserve_asset_names(
        [("SAME-NAME.jpg", "upload:should-not-reserve")]
    )
    assert duplicate[0]["asset_id"] == alias.asset_id


def test_pack_rule_crud_and_job_snapshot(settings):
    rules = PackRuleStore(settings)
    defaults = rules.list(enabled_only=True)
    assert {rule.handler for rule in defaults} >= {
        "group_by_order",
        "repeat_quantity",
        "missing_report",
    }
    custom = rules.create(
        name="人工复核说明",
        description="结果交付前人工复核",
        handler="note",
    )
    changed = rules.update(
        custom.id,
        name="人工终检",
        description=custom.description,
        handler="note",
        enabled=True,
        sort_order=900,
        config={},
    )
    assert changed and changed.name == "人工终检"
    snapshot = rules.snapshots([custom.id])
    assert snapshot[0]["handler"] == "note"
    assert rules.delete(custom.id)


def test_eve35_columns_and_selected_pack_rules(settings):
    xlsx = settings.tmp_dir / "eve35.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["订单号", "SKU", "数量", "地址", "关键词"])
    ws.append([1001, "HQT10001", 2, "上海*", "front"])
    wb.save(xlsx)
    rows = read_excel_rows(xlsx)
    assert rows[0].order_id == "1001"
    assert rows[0].quantity == 2
    assert rows[0].address == "上海*"
    assert rows[0].keyword == "front"
