"""Environment-driven configuration for the BPD MCP server."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _expand(path: str | Path) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(path)))).resolve()


class Settings(BaseSettings):
    """All env-driven settings. Values come from os.environ and (optionally) a .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Kiteworks Auth ---
    kiteworks_base_url: str = Field(default="https://securesharek.target.com")
    kiteworks_username: str | None = None
    kiteworks_password: SecretStr | None = None
    kiteworks_client_id: str = Field(default="1c9f2b24-847e-5ee6-acc4-f03c8da0cfba")
    kiteworks_client_secret: SecretStr = Field(default=SecretStr("2bLyg*oqja"))
    kiteworks_oauth_scope: str = Field(default="*/*/*")
    kiteworks_api_version: str = Field(default="15")

    # --- Vendor identity ---
    bpd_vendor_id: str = Field(default="139440")
    bpd_vendor_tier: Literal["BV", "BR", "CC"] = Field(default="BV")

    # --- Local storage ---
    bpd_data_dir: str = Field(default="~/.bpd-mcp")
    bpd_raw_dir: str | None = None
    bpd_extract_dir: str | None = None
    bpd_db_path: str | None = None
    bpd_token_file: str | None = None

    # --- Behavior ---
    bpd_auto_sync_on_start: bool = False
    bpd_log_level: str = Field(default="INFO")
    bpd_http_timeout: float = 60.0
    bpd_max_parallel_downloads: int = 4
    bpd_raw_dir_max_bytes: int = 5 * 1024 * 1024 * 1024  # 5 GiB

    # ---------- normalized accessors ----------

    @property
    def base_url(self) -> str:
        return self.kiteworks_base_url.rstrip("/")

    @property
    def data_dir(self) -> Path:
        return _expand(self.bpd_data_dir)

    @property
    def raw_dir(self) -> Path:
        return _expand(self.bpd_raw_dir) if self.bpd_raw_dir else self.data_dir / "raw"

    @property
    def extract_dir(self) -> Path:
        return _expand(self.bpd_extract_dir) if self.bpd_extract_dir else self.data_dir / "extracted"

    @property
    def db_path(self) -> Path:
        return _expand(self.bpd_db_path) if self.bpd_db_path else self.data_dir / "bpd.duckdb"

    @property
    def token_file(self) -> Path:
        return _expand(self.bpd_token_file) if self.bpd_token_file else self.data_dir / "tokens.json"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @field_validator("kiteworks_base_url")
    @classmethod
    def _check_host(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("KITEWORKS_BASE_URL must include scheme (https://...)")
        return v.rstrip("/")

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.raw_dir, self.extract_dir, self.log_dir):
            d.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings(refresh: bool = False) -> Settings:
    """Return the process-wide Settings instance (constructed lazily)."""
    global _settings
    if _settings is None or refresh:
        _settings = Settings()
    return _settings
