import pytest
from pydantic import ValidationError

from asset_hub.config import Settings, library_mount_available, load_settings


def test_settings_defaults(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "data_root: /tmp/asset-hub-test\nlocal_only: true\nprovider: mock\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("ASSET_HUB_DATA", raising=False)
    s = load_settings(cfg)
    assert s.local_only is True
    assert s.provider == "mock"
    assert s.job_retention_hours == 24
    assert s.api.x_accel is True
    assert s.finalized_dir.as_posix().endswith("/finalized")


def test_settings_env_override(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("data_root: /tmp/old\n", encoding="utf-8")
    monkeypatch.setenv("ASSET_HUB_DATA", str(tmp_path / "nvme"))
    s = load_settings(cfg)
    assert s.data_root == tmp_path / "nvme"


def test_ticket_batch_cannot_exceed_upstream_contract():
    with pytest.raises(ValidationError):
        Settings.model_validate({"sync": {"ticket_batch_size": 51}})


def test_library_mount_check_is_explicit(monkeypatch):
    optional = Settings.model_validate({"library_mount_required": False})
    required = Settings.model_validate({"library_mount_required": True})
    monkeypatch.setattr("asset_hub.config.os.path.ismount", lambda _path: False)

    assert library_mount_available(optional) is True
    assert library_mount_available(required) is False
