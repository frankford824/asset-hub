from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_catalog_writers_share_process_lock():
    index_unit = (ROOT / "deploy/systemd/asset-hub-index.service").read_text()
    sync_unit = (ROOT / "deploy/systemd/asset-hub-sync.service").read_text()
    lock = "/run/asset-hub/catalog.lock"

    assert "/usr/bin/flock --exclusive --wait 7200" in index_unit
    assert "/usr/bin/flock --exclusive --wait 7200" in sync_unit
    assert lock in index_unit
    assert lock in sync_unit


def test_index_timer_waits_from_completion():
    timer = (ROOT / "deploy/systemd/asset-hub-index.timer").read_text()

    assert "OnUnitInactiveSec=30min" in timer
    assert "OnUnitActiveSec" not in timer
