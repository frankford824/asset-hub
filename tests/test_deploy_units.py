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


def test_index_timer_runs_off_hours():
    timer = (ROOT / "deploy/systemd/asset-hub-index.timer").read_text()

    assert "OnCalendar=*-*-* 03:30:00" in timer
    assert "RandomizedDelaySec=5min" in timer
    assert "OnUnitInactiveSec" not in timer
    assert "OnUnitActiveSec" not in timer


def test_nginx_download_path_uses_sendfile_without_directio():
    nginx = (ROOT / "deploy/nginx/asset-hub.conf").read_text()

    assert "reuseport backlog=4096" in nginx
    assert "keepalive 128" in nginx
    assert "sendfile_max_chunk 2m" in nginx
    assert "directio" not in nginx


def test_worker_pool_has_one_graceful_shutdown_deadline():
    unit = (ROOT / "deploy/systemd/asset-hub-worker.service").read_text()

    assert "KillMode=mixed" in unit
    assert "TimeoutStopSec=300" in unit


def test_sync_timer_waits_from_completion():
    timer = (ROOT / "deploy/systemd/asset-hub-sync.timer").read_text()

    assert "OnUnitInactiveSec=5min" in timer
    assert "OnUnitActiveSec" not in timer
