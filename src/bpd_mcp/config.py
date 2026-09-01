"""Environment-driven configuration for the BPD MCP server.

The server reads Target BPD data from BigQuery (`biom-reporting-s26`), which an
independent Kiteworks -> GCS -> BigQuery pipeline loads daily around 06:47 UTC.
It no longer downloads, parses or stores anything itself, so everything that
configured that half — Kiteworks host/credentials, the raw/extract directories,
the DuckDB file path, the token cache, download parallelism, auto-sync,
auto-backup — is gone.

Two kinds of local storage survive, both write-only outputs rather than a data
store: CSV exports under `data_dir/exports` and logs under `data_dir/logs`.

CREDENTIALS ARE DELIBERATELY NOT SETTINGS FIELDS. `GOOGLE_APPLICATION_CREDENTIALS`
and `GCP_SA_KEY_B64` are read straight from `os.environ` by
`bq.resolve_credentials()`. A `SecretStr` field would still reach logs and error
payloads through `model_dump()`, and the base64 blob is a full private key.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .bq import BQ_LOCATION_DEFAULT, BQ_PROJECT_DEFAULT


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

    # --- BigQuery ---
    bpd_bq_project: str = Field(default=BQ_PROJECT_DEFAULT)
    # Location is REQUIRED and non-optional in practice: omitting it makes
    # INFORMATION_SCHEMA silently return zero rows, which presents as "the table
    # has no columns" rather than as an error. Validated non-empty below.
    bpd_bq_location: str = Field(default=BQ_LOCATION_DEFAULT)

    # Hard ceiling applied to every job via `maximum_bytes_billed`. BigQuery
    # rejects an over-limit job as an HTTP 500 carrying
    # `reason: bytesBilledLimitExceeded` (NOT a 403), which `bq.QueryTooExpensive`
    # detects on the message string.
    bpd_bq_max_bytes_billed: int = Field(default=20 * 1024 * 1024 * 1024)  # 20 GiB
    # Soft threshold: the pre-flight dry-run gate logs a warning above this.
    bpd_bq_warn_bytes: int = Field(default=1 * 1024 * 1024 * 1024)  # 1 GiB

    # TTLs for the two metadata caches that are not free. The date-range sweep
    # is one combined UNION ALL job over every logical table (~527 MB); at 900 s
    # a heavy interactive session refreshes it about four times an hour. Row
    # counts come from `__TABLES__` and cost nothing, so their TTL only exists
    # to keep the numbers moving.
    bpd_bq_daterange_ttl_s: int = Field(default=900)
    bpd_bq_rowcount_ttl_s: int = Field(default=300)

    # --- Vendor identity ---
    bpd_vendor_id: str = Field(default="139440")
    bpd_vendor_tier: Literal["BV", "BR", "CC"] = Field(default="BV")

    # --- Local storage (outputs only — there is no local data store any more) ---
    bpd_data_dir: str = Field(default="~/.bpd-mcp")

    # Ceiling on `bpd_export_query_to_csv`. Lowered from the DuckDB-era
    # 1,000,000 because on per-byte billing an unguarded export is a money
    # question, not a disk question.
    bpd_export_max_rows: int = Field(default=200_000)

    # --- Behavior ---
    bpd_log_level: str = Field(default="INFO")

    # ---------- normalized accessors ----------

    @property
    def data_dir(self) -> Path:
        return _expand(self.bpd_data_dir)

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def exports_dir(self) -> Path:
        """Where `bpd_export_query_to_csv` writes. The last surviving local-storage use."""
        return self.data_dir / "exports"

    @field_validator("bpd_bq_location")
    @classmethod
    def _check_location(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError(
                "BPD_BQ_LOCATION must be set (e.g. 'us-central1'). An empty location "
                "makes INFORMATION_SCHEMA silently return zero rows instead of failing."
            )
        return v.strip()

    @field_validator("bpd_bq_project")
    @classmethod
    def _check_project(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("BPD_BQ_PROJECT must be set (e.g. 'biom-reporting-s26').")
        return v.strip()

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.log_dir, self.exports_dir):
            d.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings(refresh: bool = False) -> Settings:
    """Return the process-wide Settings instance (constructed lazily)."""
    global _settings
    if _settings is None or refresh:
        _settings = Settings()
    return _settings
