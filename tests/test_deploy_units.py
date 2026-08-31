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
    assert "proxy_buffering off" in nginx
    assert "proxy_max_temp_file_size 0" in nginx
    assert "directio" not in nginx


def test_api_service_forces_x_accel_downloads():
    unit = (ROOT / "deploy/systemd/asset-hub-api.service").read_text()

    assert "Environment=ASSET_HUB_X_ACCEL=1" in unit


def test_scoped_passwordless_sudo_does_not_grant_all_commands():
    script = (ROOT / "scripts/apply_asset_hub_ops_fix.sh").read_text()

    assert "ASSET_HUB_OPERATIONS" in script
    assert "NOPASSWD: ASSET_HUB_OPERATIONS" in script
    assert "NOPASSWD: ALL" not in script
    assert "visudo -cf" in script
    assert "job_retention_hours: 24" in script


def test_worker_pool_has_one_graceful_shutdown_deadline():
    unit = (ROOT / "deploy/systemd/asset-hub-worker.service").read_text()

    assert "KillMode=mixed" in unit
    assert "TimeoutStopSec=300" in unit


def test_library_search_supports_enter_and_clear():
    app = (ROOT / "web/dist/app.js").read_text()

    assert 'event.key === "Enter"' in app
    assert 'librarySearch.addEventListener("search", submitLibrarySearch)' in app


def test_frontend_blocks_packaging_when_library_mount_is_offline():
    app = (ROOT / "web/dist/app.js").read_text()

    assert 'status.library_mount_required && !status.library_mounted' in app
    assert '"素材盘离线"' in app
    assert '$("#pack-submit").disabled = mountOffline' in app


def test_library_download_uses_native_links_for_single_and_batch():
    app = (ROOT / "web/dist/app.js").read_text()
    html = (ROOT / "web/dist/index.html").read_text()

    assert '<a id="download-selected"' in html
    assert 'download.href = downloadUrl([...state.selected][0])' in app
    assert 'download.href = state.batchDownloadUrl' in app
    assert 'download.download = "素材下载.zip"' in app
    assert '正在准备 ${count} 项' in app
    assert 'document.createElement("a")' not in app


def test_pack_submit_explains_duplicate_output_behavior():
    app = (ROOT / "web/dist/app.js").read_text()

    assert "重复行按次数分别输出" in app
    assert "个完全重复行已合并" not in app


def test_deploy_preserves_nginx_traversal_permission():
    install = (ROOT / "deploy/install.sh").read_text()
    deploy = (ROOT / "scripts/deploy_to_ybyc.sh").read_text()

    assert 'chmod 751 "${ASSET_HUB_ROOT}"' in install
    assert "sudo chmod 0751 ${ROOT}" in deploy


def test_data_consumers_require_the_library_mount_guard():
    for name in (
        "asset-hub-worker.service",
        "asset-hub-sync.service",
        "asset-hub-external-follow.service",
        "asset-hub-index.service",
    ):
        unit = (ROOT / "deploy/systemd" / name).read_text()
        assert "Requires=asset-hub-library-mount.service" in unit
        assert "After=asset-hub-library-mount.service" in unit


def test_mount_guard_repairs_only_a_confirmed_dirty_ntfs_volume():
    guard = (ROOT / "deploy/ensure-library-mount.sh").read_text()

    assert 'findmnt --fstab --evaluate' in guard
    assert '[[ -b "$device" ]]' in guard
    assert '[[ "$fstype" == "ntfs" ]]' in guard
    assert 'volume is dirty' in guard
    assert "ntfs-3g" in (ROOT / "deploy/install.sh").read_text()
    assert 'ntfsfix -d "$device"' in guard
    assert "mount -o force" not in guard


def test_mount_guard_is_rechecked_every_minute():
    timer = (ROOT / "deploy/systemd/asset-hub-library-mount.timer").read_text()
    service = (ROOT / "deploy/systemd/asset-hub-library-mount.service").read_text()

    assert "OnUnitInactiveSec=60s" in timer
    assert "Persistent=true" in timer
    assert "RemainAfterExit" not in service


def test_sync_timer_waits_from_completion():
    timer = (ROOT / "deploy/systemd/asset-hub-sync.timer").read_text()

    assert "OnUnitInactiveSec=5min" in timer
    assert "OnUnitActiveSec" not in timer


def test_external_follow_timer_runs_every_ten_seconds_under_the_catalog_lock():
    timer = (ROOT / "deploy/systemd/asset-hub-external-follow.timer").read_text()
    service = (ROOT / "deploy/systemd/asset-hub-external-follow.service").read_text()

    assert "OnUnitInactiveSec=10s" in timer
    assert "/run/asset-hub/catalog.lock" in service
    assert "asset-hub-external-follow" in service
