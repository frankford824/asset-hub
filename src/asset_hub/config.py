from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class HttpConfig(BaseModel):
    base_url: str = ""
    token: str = ""
    timeout_sec: int = 30


class SyncConfig(BaseModel):
    kinds: list[str] = Field(default_factory=lambda: ["finalized"])
    interval_sec: int = 300
    ticket_batch_size: int = Field(default=50, ge=1, le=50)
    verify_interval_sec: int = Field(default=86400, ge=60)
    ignore_globs: list[str] = Field(
        default_factory=lambda: ["Thumbs.db", "desktop.ini", "._*"]
    )


class ApiConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8080
    workers: int = Field(default=4, ge=1, le=16)
    backlog: int = Field(default=2048, ge=128, le=65535)
    limit_concurrency: int = Field(default=512, ge=100, le=4096)


class Settings(BaseModel):
    library_root: Path = Path("/home/resourse")
    data_root: Path = Path("/var/lib/asset-hub")
    local_only: bool = True
    workers: int = 3
    pack_workers: int = Field(default=4, ge=1, le=12)
    provider: str = "mock"  # mock | http
    http: HttpConfig = Field(default_factory=HttpConfig)
    sync: SyncConfig = Field(default_factory=SyncConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)

    @property
    def db_path(self) -> Path:
        return self.data_root / "db" / "catalog.sqlite3"

    @property
    def jobs_db_path(self) -> Path:
        return self.data_root / "db" / "jobs.sqlite3"

    @property
    def finalized_dir(self) -> Path:
        return self.data_root / "finalized"

    @property
    def archive_dir(self) -> Path:
        """P4 冷库缓存根（kind=archive）；默认不启用同步。"""
        return self.data_root / "archive"

    @property
    def jobs_dir(self) -> Path:
        return self.data_root / "jobs"

    @property
    def tmp_dir(self) -> Path:
        return self.data_root / "tmp"


def _default_config_path() -> Path:
    return Path(os.environ.get("ASSET_HUB_CONFIG", "/etc/asset-hub/config.yaml"))


def load_settings(path: Path | None = None) -> Settings:
    cfg_path = path or _default_config_path()
    data: dict[str, Any] = {}
    if cfg_path.is_file():
        with cfg_path.open("r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"配置必须是 mapping: {cfg_path}")
            data = loaded

    # 环境变量可覆盖关键路径
    if os.environ.get("ASSET_HUB_DATA"):
        data["data_root"] = os.environ["ASSET_HUB_DATA"]
    if os.environ.get("ASSET_HUB_LIBRARY"):
        data["library_root"] = os.environ["ASSET_HUB_LIBRARY"]

    return Settings.model_validate(data)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()


def ensure_data_dirs(settings: Settings | None = None) -> Settings:
    s = settings or get_settings()
    for d in (
        s.data_root,
        s.data_root / "db",
        s.finalized_dir,
        s.archive_dir,
        s.jobs_dir,
        s.tmp_dir,
    ):
        d.mkdir(parents=True, exist_ok=True)
    return s
