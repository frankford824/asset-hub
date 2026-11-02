from collections import defaultdict
from dataclasses import replace
import time

import pytest

from asset_hub.catalog.db import Catalog
from asset_hub.config import Settings, ensure_data_dirs
from asset_hub.sync import main as sync
from asset_hub.sync.provider import (
    ExternalDownloadTicket, ExternalManifestItem, ExternalManifestResult,
)


@pytest.fixture()
def cached(tmp_path, monkeypatch):
    settings = ensure_data_dirs(Settings(
        library_root=tmp_path / "library", data_root=tmp_path / "data",
    ))
    settings.library_root.mkdir()
    path = settings.library_root / "HSC22508.jpg"
    path.write_bytes(b"current")
    catalog = Catalog(settings)
    item = ExternalManifestItem(
        701, "origin", path.name, path.name, "image/jpeg", 7,
        "external/current.jpg", 100, 100, False,
    )
    catalog.apply_external_manifest([item], "manifest")
    catalog.set_sync_state("external", manifest_id="manifest", etag='W/"manifest"', ready=True)
    monkeypatch.setattr(sync, "ensure_data_dirs", lambda: settings)
    return settings, catalog, item, path


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize("status", ["size_mismatch", "missing", "error", "pending", "tombstone"])
def test_replayed_metadata_requires_ticket_for_unverified_retained_bytes(cached, full, status):
    settings, catalog, item, path = cached
    catalog.mark_asset_status("external:701", status, retryable=False, error="validation failed")
    if full:
        catalog.apply_external_manifest([item], "manifest")
    else:
        catalog.apply_external_changes([item], "manifest")
    assert catalog.get_asset("external:701").status == "pending"
    assert not catalog.external_cache_complete("manifest")

    class Provider:
        def get_external_download_tickets(self, ids):
            assert ids == [701]
            return [ExternalDownloadTicket(701, "size_mismatch", expected_size=7, actual_size=3)]

    stats = defaultdict(int)
    assert not sync._process_external_change_tickets(Provider(), catalog, [item], settings, stats)
    assert stats["requested"] == stats["size_mismatch"] == 1
    assert catalog.get_asset("external:701").status == "size_mismatch"
    assert path.read_bytes() == b"current"


def test_304_retries_legacy_nonretryable_size_mismatch_until_ready(cached, monkeypatch):
    _, catalog, _, path = cached
    catalog.mark_asset_status("external:701", "size_mismatch", retryable=False)
    catalog.set_sync_state("external", ready=False, verified=True)

    class Provider:
        status = "size_mismatch"
        requests = 0

        def get_external_manifest(self, etag):
            assert etag == 'W/"manifest"'
            return ExternalManifestResult(manifest_id="manifest", etag=etag, not_modified=True)

        def get_external_download_tickets(self, ids):
            assert ids == [701]
            self.requests += 1
            return [ExternalDownloadTicket(
                701, self.status, relative_path=path.name, storage_key="external/current.jpg",
                expected_size=7, actual_size=7 if self.status == "ready" else 3,
                etag="validated-etag", download_url="https://objects.invalid/current",
            )]

    provider = Provider()
    for _ in range(2):
        result = sync.sync_external_once(provider)
        assert result["requested"] == result["size_mismatch"] == 1
        assert not result["sync_complete"]
        assert not catalog.get_sync_state("external")["ready"]
        assert path.read_bytes() == b"current"

    downloads = []
    def download(destination, asset, ticket, settings):
        downloads.append(ticket.etag)
        destination.write_bytes(b"current")
    monkeypatch.setattr(sync, "_download_ready_ticket", download)
    provider.status = "ready"
    result = sync.sync_external_once(provider)
    assert result["requested"] == result["ready"] == 1
    assert result["sync_complete"] and downloads == ["validated-etag"]
    assert catalog.get_asset("external:701").etag == "validated-etag"
    assert sync.sync_external_once(provider)["requested"] == 0
    assert provider.requests == 3


def test_unverified_adopted_cache_gets_initial_full_verification(cached, monkeypatch):
    _, catalog, _, path = cached
    assert catalog.get_sync_state("external")["last_verified_at"] == 0

    class Provider:
        requests = 0
        def get_external_manifest(self, etag):
            return ExternalManifestResult(manifest_id="manifest", etag=etag, not_modified=True)
        def get_external_download_tickets(self, ids):
            self.requests += 1
            assert ids == [701]
            return [ExternalDownloadTicket(701, "size_mismatch", expected_size=7, actual_size=3)]

    provider = Provider()
    before = time.time()
    result = sync.sync_external_once(provider)
    assert result["requested"] == result["size_mismatch"] == provider.requests == 1
    assert not result["sync_complete"]
    assert catalog.get_sync_state("external")["last_verified_at"] >= before
    assert catalog.get_asset("external:701").status == "size_mismatch"
    assert path.read_bytes() == b"current"


def test_replayed_verified_cache_remains_reusable(cached):
    _, catalog, item, _ = cached
    catalog.mark_asset_status("external:701", "ready", etag="verified")
    catalog.apply_external_manifest([item], "manifest")
    catalog.apply_external_changes([item], "manifest")
    assert catalog.get_asset("external:701").etag == "verified"
    assert catalog.external_cache_complete("manifest")
    assert not catalog.ticket_candidates(kind="external")


def test_same_size_source_replacement_stays_pending_across_replay(cached):
    _, catalog, item, path = cached
    catalog.mark_asset_status("external:701", "ready", etag="old-source")
    changed = replace(item, source_modified_at=200, record_updated_at=200)
    catalog.apply_external_changes([changed], "manifest")
    catalog.apply_external_changes([changed], "manifest")
    assert path.read_bytes() == b"current"
    assert catalog.get_asset("external:701").status == "pending"
    assert not catalog.external_cache_complete("manifest")
