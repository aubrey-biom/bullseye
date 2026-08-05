"""`bpd-sync` headless CLI tests (Patch #15).

The exit-code contract is the interface a scheduler consumes, so it is pinned
here: 0 ok · 1 file(s) failed · 2 health fail · 3 fatal · 75 warehouse locked.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest
from pydantic import SecretStr

from bpd_mcp import cli_sync
from bpd_mcp.config import Settings
from bpd_mcp.sync import SyncResult
from bpd_mcp.warehouse import Warehouse


def _args(**overrides) -> argparse.Namespace:
    base = {
        "datasets": None,
        "dry_run": False,
        "skip_health": True,
        "triggered_by": "test-cli",
        "lock_retries": 1,
        "lock_retry_delay": 0.0,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        kiteworks_base_url="https://securesharek.target.com",
        kiteworks_username="u@example.com",
        kiteworks_password=SecretStr("pw"),
        kiteworks_client_id="cid",
        kiteworks_client_secret=SecretStr("csec"),
        bpd_data_dir=str(tmp_path),
        bpd_vendor_id="139440",
    )


def _result(**counts) -> SyncResult:
    now = datetime.now(UTC)
    return SyncResult(
        started_at=now,
        finished_at=now,
        triggered_by="test-cli",
        folder_id="F1",
        **counts,
    )


@pytest.fixture
def patched(tmp_path, monkeypatch):
    """Point the CLI at a tmp warehouse and stub out the network."""
    s = _settings(tmp_path)
    monkeypatch.setattr("bpd_mcp.config.get_settings", lambda *a, **k: s)

    class _FakeHttp:
        async def aclose(self) -> None:
            return None

    monkeypatch.setattr("bpd_mcp.client.make_http_client", lambda _s: _FakeHttp())
    monkeypatch.setattr(
        "bpd_mcp.auth.AuthManager.load_from_disk", lambda _s, _http: object()
    )
    monkeypatch.setattr(
        "bpd_mcp.client.KiteworksClient", lambda *a, **k: object()
    )
    return s


async def test_exit_ok_on_clean_sync(patched, monkeypatch, capsys) -> None:
    async def _sync(*a, **k):
        return _result(files_found=5, files_loaded=2, files_skipped=3)

    monkeypatch.setattr("bpd_mcp.sync.sync_new_files", _sync)
    code = await cli_sync._run(_args())
    assert code == cli_sync.EXIT_OK
    out = capsys.readouterr().out
    assert "found=5 loaded=2 failed=0 skipped=3" in out


async def test_exit_1_when_a_file_fails(patched, monkeypatch, capsys) -> None:
    async def _sync(*a, **k):
        r = _result(files_found=2, files_loaded=1, files_failed=1)
        from bpd_mcp.sync import FileOutcome

        r.outcomes.append(
            FileOutcome(
                file_id="X",
                file_name="bad.zip",
                dataset="sales_weekly",
                status="failed",
                error="parse: boom",
            )
        )
        return r

    monkeypatch.setattr("bpd_mcp.sync.sync_new_files", _sync)
    code = await cli_sync._run(_args())
    assert code == cli_sync.EXIT_FILES_FAILED
    # The failing file must be named on stderr for the scheduler's log.
    assert "FAILED bad.zip" in capsys.readouterr().err


async def test_exit_2_when_health_check_fails(patched, monkeypatch) -> None:
    async def _sync(*a, **k):
        return _result(files_found=1, files_loaded=1)

    async def _health(**kwargs):
        from bpd_mcp.schemas import ToolResponse

        return ToolResponse(
            ok=True, format="json", rendered="", data={"overall_status": "fail"}
        )

    monkeypatch.setattr("bpd_mcp.sync.sync_new_files", _sync)
    monkeypatch.setattr("bpd_mcp.tools.admin.health_check", _health)
    code = await cli_sync._run(_args(skip_health=False))
    assert code == cli_sync.EXIT_HEALTH_FAIL


async def test_exit_75_when_warehouse_is_locked(patched, monkeypatch) -> None:
    """The MCP server holding the single DuckDB writer is expected, not an
    error: skip the run so the next one picks the work up."""
    calls: list[int] = []

    def _locked(*a, **k):
        calls.append(1)
        raise duckdb.IOException(
            "Could not set lock on file: Conflicting lock is held"
        )

    monkeypatch.setattr("bpd_mcp.warehouse.Warehouse", _locked)
    code = await cli_sync._run(_args(lock_retries=3, lock_retry_delay=0.0))
    assert code == cli_sync.EXIT_LOCKED
    assert len(calls) == 3, "locked runs must retry up to --lock-retries"


async def test_non_lock_duckdb_error_is_fatal_not_skipped(
    patched, monkeypatch
) -> None:
    def _boom(*a, **k):
        raise duckdb.IOException("disk I/O error: corrupt database")

    monkeypatch.setattr("bpd_mcp.warehouse.Warehouse", _boom)
    with pytest.raises(duckdb.IOException):
        await cli_sync._run(_args())


async def test_dry_run_skips_health_and_reports(patched, monkeypatch, capsys) -> None:
    seen: dict[str, object] = {}

    async def _sync(*a, **k):
        seen.update(k)
        return _result(files_found=4)

    async def _health(**kwargs):  # must NOT be called on a dry run
        raise AssertionError("health check must not run during --dry-run")

    monkeypatch.setattr("bpd_mcp.sync.sync_new_files", _sync)
    monkeypatch.setattr("bpd_mcp.tools.admin.health_check", _health)
    code = await cli_sync._run(_args(dry_run=True, skip_health=False))
    assert code == cli_sync.EXIT_OK
    assert seen["dry_run"] is True
    assert "(dry-run)" in capsys.readouterr().out


async def test_datasets_filter_is_passed_through(patched, monkeypatch) -> None:
    seen: dict[str, object] = {}

    async def _sync(*a, **k):
        seen.update(k)
        return _result()

    monkeypatch.setattr("bpd_mcp.sync.sync_new_files", _sync)
    await cli_sync._run(_args(datasets=["sales_weekly", "sales_daily"]))
    assert seen["datasets"] == {"sales_weekly", "sales_daily"}
    assert seen["triggered_by"] == "test-cli"


def test_lock_error_detection() -> None:
    assert cli_sync._is_lock_error(RuntimeError("Conflicting lock is held"))
    assert cli_sync._is_lock_error(RuntimeError("Could not set lock on file"))
    assert not cli_sync._is_lock_error(RuntimeError("syntax error near FROM"))


def test_warehouse_lock_is_real_not_hypothetical(tmp_path: Path) -> None:
    """Guard the premise behind exit 75: a second writable connection to the
    same DuckDB file from ANOTHER PROCESS is refused, and the refusal text is
    what _is_lock_error matches.

    Must be cross-process: within one process DuckDB hands back a connection to
    the same cached instance and no lock error occurs — so a same-process
    assertion would pass while proving nothing about the launchd scenario
    (where the MCP server is a separate process).
    """
    import subprocess
    import sys

    db = tmp_path / "bpd.duckdb"
    first = Warehouse(db)
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                f"import duckdb; duckdb.connect({str(db)!r}, read_only=False)",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode != 0, "a second process must be refused the write lock"
        assert cli_sync._is_lock_error(RuntimeError(proc.stderr)), proc.stderr
    finally:
        first.close()


def test_console_script_is_registered() -> None:
    """pyproject must expose `bpd-sync` — the launchd job invokes it by name."""
    import tomllib

    root = Path(__file__).resolve().parents[1]
    with open(root / "pyproject.toml", "rb") as f:
        pyproject = tomllib.load(f)
    assert pyproject["project"]["scripts"]["bpd-sync"] == "bpd_mcp.cli_sync:main"


def test_launchd_template_and_installer_are_consistent() -> None:
    root = Path(__file__).resolve().parents[1]
    template = (root / "scripts" / "com.biom.bpd-sync.plist.template").read_text()
    installer = (root / "scripts" / "install_launchd.sh").read_text()
    # Every placeholder in the template must be substituted by the installer.
    for placeholder in ("__UV_BIN__", "__REPO_DIR__", "__HOME__", "__HOUR__", "__MINUTE__"):
        assert placeholder in template
        assert placeholder in installer, f"{placeholder} never substituted"
    # No credentials may be baked into the plist.
    assert "KITEWORKS_PASSWORD" not in template
    assert "KITEWORKS_USERNAME" not in template
    # Installing must not trigger an unrequested sync.
    assert "<key>RunAtLoad</key>\n    <false/>" in template
