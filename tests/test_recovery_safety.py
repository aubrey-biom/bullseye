"""Patch #9 tests: warehouse backups, destructive-refresh guardrails, local
reingest (recovery from the raw zip archive), and raw-dir eviction protection.

Context: Kiteworks retains only ~2 weeks of files, so the local warehouse and
`raw_dir` zips are the ONLY archive of older history. A `full=true` refresh in
July 2026 destroyed a validated 17-month backfill; these guardrails exist so
that can never silently happen again.
"""

from __future__ import annotations

import os
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import polars as pl
import pytest
import respx
from pydantic import SecretStr

from bpd_mcp import sync as sync_mod
from bpd_mcp.auth import AuthManager, TokenBundle
from bpd_mcp.client import KiteworksClient
from bpd_mcp.config import Settings
from bpd_mcp.parsers import derive_duckdb_schema
from bpd_mcp.schemas import RefreshDatasetInput
from bpd_mcp.sync import (
    CONFIRM_DESTRUCTIVE_PHRASE,
    RefreshWouldLoseHistory,
    _enforce_raw_dir_cap,
    create_backup,
    refresh_dataset,
    reingest_local_files,
    sync_new_files,
)
from bpd_mcp.tools import sync as sync_tools
from bpd_mcp.warehouse import Warehouse

BASE = "https://securesharek.target.com"

# Real current-feed sales_weekly header (same header in HISTORY files).
SALES_WEEKLY_HDR = (
    "SALES_DATE\tVENDOR_ID\tBARCODE\tTCIN\tDPCI\tORIGINATION_CHANNEL\t"
    "REPORTING_CHANNEL\tFULFILLMENT_TYPE\tLOCATION_ID\tSALE_AMOUNT\tSALE_QUANTITY"
)

GM_COLS = (
    "VENDOR_ID\tTCIN\tDPCI\tCHANNEL_ORIGINATED\tLOCATION_ID_ORIGINATED\t"
    "LOCATION_ID\tCHANNEL_FULFILLED\tFULFILLMENT_TYPE\tFULFILLMENT_SUBTYPE\t"
    "NET_SALES_A\tNET_SALES_Q\tADJUSTED_GROSS_MARGIN_A"
)
GM_VALS = "2003081\t89854823\t003-02-1327\tSTORE\t2036\t2036\tSTORE\tNA\tNA\t19.99\t1.0\t8.5"


def _sales_row(week: str, *, tcin: int = 89854823, amount: float = 19.99) -> str:
    return (
        f"{week}\t2003081\t850036134121\t{tcin}\t003-02-1327\tSTORE\tSTR\t"
        f"CARRYOUT\t918\t{amount}\t1.0"
    )


def _zip(path: Path, body: str, inner: str = "data.txt") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(inner, body)
    return path


def _settings(tmp_path: Path, **overrides) -> Settings:
    kw: dict = {
        "kiteworks_base_url": BASE,
        "kiteworks_username": "u@example.com",
        "kiteworks_password": SecretStr("pw"),
        "kiteworks_client_id": "cid",
        "kiteworks_client_secret": SecretStr("csec"),
        "bpd_data_dir": str(tmp_path),
        "bpd_vendor_id": "139440",
    }
    kw.update(overrides)
    s = Settings(**kw)
    s.ensure_dirs()
    return s


def _seed_sales_weekly(
    wh: Warehouse, *, week: str, file_name: str, file_id: str = "OLD1"
) -> None:
    """One loaded sales_weekly row plus its ledger entry, as if synced earlier."""
    df = pl.DataFrame(
        {
            "tcin": [89854823],
            "location_id": [918],
            "sales_date": [date.fromisoformat(week)],
            "sale_amount": [19.99],
            "sale_quantity": [1.0],
        }
    )
    cols = derive_duckdb_schema(df)
    wh.ensure_data_table("sales_weekly", cols)
    wh.upsert_dataframe(
        "sales_weekly", df, primary_key=("tcin", "location_id", "sales_date")
    )
    wh.register_schema("sales_weekly", cols, ("tcin", "location_id", "sales_date"))
    wh.ledger_upsert(
        {
            "file_id": file_id,
            "file_name": file_name,
            "folder_id": "F1",
            "dataset": "sales_weekly",
            "file_date": date.fromisoformat(week),
            "bytes": 100,
            "fingerprint": "fp-old",
            "downloaded_at": datetime.now(UTC),
            "loaded_at": datetime.now(UTC),
            "row_count": 1,
            "status": "loaded",
            "error_message": None,
            "parse_method": "strict",
        }
    )


def _mock_kiteworks(children: list[dict]) -> None:
    """Stub OAuth, the top-folder listing, and folder F1's children."""
    respx.post(f"{BASE}/oauth/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "AT",
                "refresh_token": "RT",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "*/*/*",
            },
        )
    )
    respx.get(f"{BASE}/rest/folders/top").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{"id": "F1", "name": "139440", "type": "d"}],
                "metadata": {"total": 1},
            },
        )
    )
    respx.get(f"{BASE}/rest/folders/F1/children").mock(
        return_value=httpx.Response(200, json=children)
    )


def _bundle() -> TokenBundle:
    return TokenBundle(
        access_token="AT",
        refresh_token="RT",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


# ---------- backups ----------


def test_backup_to_creates_openable_consistent_copy(tmp_path: Path) -> None:
    wh = Warehouse(tmp_path / "bpd.duckdb")
    try:
        _seed_sales_weekly(
            wh, week="2026-03-28",
            file_name="BV_139440_WEEKLY_SALES_TCIN_LOC_03282026_KW.zip",
        )
        dest = tmp_path / "backups" / "copy.duckdb"
        out = wh.backup_to(dest)
        assert out == dest and dest.exists()
        # The copy must open and contain the rows even while the source
        # connection is still open (CHECKPOINT flushed the WAL first).
        bk = Warehouse(dest, read_only=True)
        try:
            _, rows = bk.execute_sql("SELECT COUNT(*) FROM sales_weekly")
            assert rows[0][0] == 1
        finally:
            bk.close()
    finally:
        wh.close()


def test_create_backup_retention_prunes_oldest(tmp_path: Path) -> None:
    s = _settings(tmp_path, bpd_backup_keep=2)
    wh = Warehouse(s.db_path)
    made: list[Path] = []
    try:
        for i in range(4):
            p = create_backup(wh, s, reason=f"r{i}")
            assert p is not None and p.exists()
            # Deterministic mtime ordering regardless of clock resolution.
            os.utime(p, (1000 + i, 1000 + i))
            made.append(p)
    finally:
        wh.close()
    left = set(s.backups_dir.glob("bpd-*.duckdb"))
    assert left == {made[2], made[3]}, "only the newest bpd_backup_keep=2 survive"


def test_create_backup_respects_toggle_but_force_overrides(tmp_path: Path) -> None:
    s = _settings(tmp_path, bpd_auto_backup=False)
    wh = Warehouse(s.db_path)
    try:
        assert create_backup(wh, s, reason="routine") is None
        assert list(s.backups_dir.glob("*.duckdb")) == []
        # Pre-destructive-delete backups must happen even when routine
        # auto-backups are disabled.
        forced = create_backup(wh, s, reason="pre-refresh-x", force=True)
        assert forced is not None and forced.exists()
    finally:
        wh.close()


async def test_sync_backs_up_before_touching_warehouse(
    tmp_path: Path, monkeypatch
) -> None:
    s = _settings(tmp_path)
    wh = Warehouse(s.db_path)

    async def _no_folder(client, vendor_id):
        return None

    monkeypatch.setattr(sync_mod, "_find_vendor_folder", _no_folder)

    reasons: list[str] = []
    real_create_backup = sync_mod.create_backup

    def counting(*args, **kwargs):
        reasons.append(kwargs.get("reason", ""))
        return real_create_backup(*args, **kwargs)

    monkeypatch.setattr(sync_mod, "create_backup", counting)
    try:
        await sync_new_files(None, wh, s, triggered_by="t")
        assert reasons == ["pre-sync"]
        assert len(list(s.backups_dir.glob("bpd-*-pre-sync.duckdb"))) == 1
        # Neither dry_run nor auto_backup=False may take a backup.
        await sync_new_files(None, wh, s, triggered_by="t", dry_run=True)
        await sync_new_files(None, wh, s, triggered_by="t", auto_backup=False)
        assert reasons == ["pre-sync"]
    finally:
        wh.close()


# ---------- destructive-refresh guardrails ----------


async def test_refresh_full_requires_confirm_phrase(tmp_path: Path) -> None:
    """Without the exact phrase the tool must refuse BEFORE touching anything —
    including the network (client=None proves no Kiteworks call is made)."""
    s = _settings(tmp_path)
    wh = Warehouse(s.db_path)
    try:
        _seed_sales_weekly(
            wh, week="2026-01-03",
            file_name="BV_139440_WEEKLY_SALES_TCIN_LOC_01032026_KW.zip",
        )
        resp = await sync_tools.refresh_dataset(
            None,
            wh,
            s,
            RefreshDatasetInput(dataset="sales_weekly", full=True),
        )
        assert resp.ok is False
        assert resp.error is not None and resp.error.code == "CONFIRM_REQUIRED"
        assert CONFIRM_DESTRUCTIVE_PHRASE in resp.error.message
        assert "bpd_reingest_local" in resp.error.message
        # The preview must show the deletion scope.
        assert "2026-01-03" in resp.error.message

        # A wrong phrase is also refused.
        resp2 = await sync_tools.refresh_dataset(
            None,
            wh,
            s,
            RefreshDatasetInput(
                dataset="sales_weekly", full=True, confirm_destructive="yes"
            ),
        )
        assert resp2.ok is False and resp2.error.code == "CONFIRM_REQUIRED"

        # Nothing was deleted.
        _, rows = wh.execute_sql("SELECT COUNT(*) FROM sales_weekly")
        assert rows[0][0] == 1
        _, led = wh.execute_sql(
            "SELECT COUNT(*) FROM _file_ledger WHERE status = 'loaded'"
        )
        assert led[0][0] == 1
    finally:
        wh.close()


@respx.mock
async def test_refresh_full_refuses_when_history_unrecoverable(tmp_path: Path) -> None:
    """Local history starts 2026-01-03 but Kiteworks only serves 2026-07-24:
    full=true must refuse even WITH the confirm phrase, and delete nothing."""
    s = _settings(tmp_path)
    _mock_kiteworks(
        [
            {
                "id": "NEWONLY",
                "name": "BV_139440_WEEKLY_SALES_TCIN_LOC_07242026_KW.zip",
                "type": "f",
                "parentId": "F1",
                "size": 100,
                "fingerprint": "fp-new",
            }
        ]
    )
    wh = Warehouse(s.db_path)
    try:
        _seed_sales_weekly(
            wh, week="2026-01-03",
            file_name="BV_139440_WEEKLY_SALES_TCIN_LOC_01032026_KW.zip",
        )
        async with httpx.AsyncClient() as http:
            auth = AuthManager(s, http, bundle=_bundle())
            client = KiteworksClient(s, auth, http)

            with pytest.raises(RefreshWouldLoseHistory):
                await refresh_dataset(
                    client, wh, s, dataset="sales_weekly", full=True
                )

            # Tool layer maps the refusal to a structured error.
            resp = await sync_tools.refresh_dataset(
                client,
                wh,
                s,
                RefreshDatasetInput(
                    dataset="sales_weekly",
                    full=True,
                    confirm_destructive=CONFIRM_DESTRUCTIVE_PHRASE,
                ),
            )
        assert resp.ok is False
        assert resp.error is not None
        assert resp.error.code == "REFRESH_WOULD_LOSE_HISTORY"
        assert "bpd_reingest_local" in resp.error.message

        # Data and ledger untouched; the refusal fires before the backup.
        _, rows = wh.execute_sql("SELECT COUNT(*) FROM sales_weekly")
        assert rows[0][0] == 1
        _, led = wh.execute_sql(
            "SELECT COUNT(*) FROM _file_ledger WHERE status = 'loaded'"
        )
        assert led[0][0] == 1
        assert list(s.backups_dir.glob("*.duckdb")) == []
    finally:
        wh.close()


@respx.mock
async def test_refresh_full_happy_path_backs_up_then_rebuilds(tmp_path: Path) -> None:
    """When Kiteworks can re-serve everything local (remote min == local min),
    full=true proceeds: mandatory pre-delete backup, then clear + re-download."""
    s = _settings(tmp_path)
    file_name = "BV_139440_WEEKLY_SALES_TCIN_LOC_03282026_KW.zip"
    _mock_kiteworks(
        [
            {
                "id": "NEW1",
                "name": file_name,
                "type": "f",
                "parentId": "F1",
                "size": 500,
                "fingerprint": "fp-new",
            }
        ]
    )
    body = (
        f"{SALES_WEEKLY_HDR}\n"
        f"{_sales_row('2026-03-28', amount=18.99)}\n"
        f"{_sales_row('2026-03-28', tcin=111, amount=20.00)}\n"
    )
    zb_path = tmp_path / "payload.zip"
    _zip(zb_path, body)
    respx.get(f"{BASE}/rest/files/NEW1/content").mock(
        return_value=httpx.Response(200, content=zb_path.read_bytes())
    )
    zb_path.unlink()

    wh = Warehouse(s.db_path)
    try:
        _seed_sales_weekly(wh, week="2026-03-28", file_name=file_name)
        async with httpx.AsyncClient() as http:
            auth = AuthManager(s, http, bundle=_bundle())
            client = KiteworksClient(s, auth, http)
            result = await refresh_dataset(
                client, wh, s, dataset="sales_weekly", full=True
            )
        assert result.files_loaded == 1, result

        # Exactly one backup: the mandatory pre-refresh one (the inner sync
        # runs with auto_backup=False), holding the PRE-DELETE contents.
        backups = list(s.backups_dir.glob("bpd-*-pre-refresh-sales_weekly.duckdb"))
        assert len(backups) == 1
        assert len(list(s.backups_dir.glob("bpd-*.duckdb"))) == 1
        bk = Warehouse(backups[0], read_only=True)
        try:
            _, old = bk.execute_sql(
                "SELECT COUNT(*), ROUND(SUM(sale_amount), 2) FROM sales_weekly"
            )
            assert old[0] == (1, 19.99)
        finally:
            bk.close()

        # Live table rebuilt from the re-download.
        _, rows = wh.execute_sql(
            "SELECT COUNT(*), ROUND(SUM(sale_amount), 2) FROM sales_weekly"
        )
        assert rows[0] == (2, 38.99)
        _, led = wh.execute_sql(
            "SELECT file_id FROM _file_ledger "
            "WHERE dataset = 'sales_weekly' AND status = 'loaded'"
        )
        assert [r[0] for r in led] == ["NEW1"]
    finally:
        wh.close()


# ---------- local reingest (the recovery path) ----------


async def test_reingest_recovers_orphan_zips_and_is_idempotent(tmp_path: Path) -> None:
    """Post-wipe scenario: empty warehouse, orphan zips in raw/. Reingest must
    load them with local: file_ids, back up first, log the sync, and be a no-op
    on the second run."""
    s = _settings(tmp_path)
    hist = "BV_139440_HISTORY_SALES_WEEKLY_01032026_KW.zip"
    reg = "BV_139440_WEEKLY_SALES_TCIN_LOC_04252026_KW.zip"
    _zip(
        s.raw_dir / hist,
        f"{SALES_WEEKLY_HDR}\n"
        f"{_sales_row('2026-01-03')}\n"
        f"{_sales_row('2026-01-03', tcin=111)}\n",
    )
    _zip(s.raw_dir / reg, f"{SALES_WEEKLY_HDR}\n{_sales_row('2026-04-25')}\n")

    wh = Warehouse(s.db_path)
    try:
        r1 = await reingest_local_files(wh, s)
        assert r1.files_found == 2
        assert (r1.files_loaded, r1.files_failed) == (2, 0), r1
        _, rows = wh.execute_sql("SELECT COUNT(*) FROM sales_weekly")
        assert rows[0][0] == 3
        _, led = wh.execute_sql(
            "SELECT file_id FROM _file_ledger WHERE status = 'loaded' ORDER BY file_id"
        )
        assert [r[0] for r in led] == [f"local:{hist}", f"local:{reg}"]
        assert any(
            "pre-reingest" in p.name for p in s.backups_dir.glob("bpd-*.duckdb")
        )
        _, logs = wh.execute_sql(
            "SELECT triggered_by, files_loaded FROM _sync_log"
        )
        assert ("bpd_reingest_local", 2) in {(r[0], r[1]) for r in logs}

        # Second run: both files already ledgered as loaded → skipped.
        r2 = await reingest_local_files(wh, s)
        assert (r2.files_loaded, r2.files_skipped) == (0, 2), r2
        _, rows2 = wh.execute_sql("SELECT COUNT(*) FROM sales_weekly")
        assert rows2[0][0] == 3
    finally:
        wh.close()


async def test_reingest_dry_run_dataset_filter_and_unknown(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    _zip(
        s.raw_dir / "BV_139440_WEEKLY_SALES_TCIN_LOC_04252026_KW.zip",
        f"{SALES_WEEKLY_HDR}\n{_sales_row('2026-04-25')}\n",
    )
    _zip(
        s.raw_dir / "BV_139440_WEEKLY_GM_TCIN_LOC_04252026_KW.zip",
        f"FISCAL_WEEK_END_D\t{GM_COLS}\n2026-04-25\t{GM_VALS}\n",
    )
    _zip(s.raw_dir / "random_stuff.zip", "A|B\n1|2\n")

    wh = Warehouse(s.db_path)
    try:
        dry = await reingest_local_files(wh, s, dry_run=True)
        assert dry.files_found == 3
        assert dry.files_unknown == 1
        assert {o.status for o in dry.outcomes} == {"unknown_pattern", "dry_run"}
        _, t = wh.execute_sql(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_name IN ('sales_weekly', 'gross_margin')"
        )
        assert t[0][0] == 0, "dry_run must not create tables"

        r = await reingest_local_files(wh, s, datasets={"gross_margin"})
        assert r.files_loaded == 1
        _, t2 = wh.execute_sql(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_name = 'sales_weekly'"
        )
        assert t2[0][0] == 0, "dataset filter must leave other zips untouched"
        _, gm = wh.execute_sql("SELECT COUNT(*) FROM gross_margin")
        assert gm[0][0] == 1
    finally:
        wh.close()


async def test_reingest_orders_by_file_date_so_regular_generation_wins(
    tmp_path: Path,
) -> None:
    """Same fiscal week present in a HISTORY zip and a regular weekly zip: the
    (file_date, name) ascending order loads HISTORY first, so the regular
    generation lands last and replace_scope leaves ITS rows for the week —
    matching what live syncs would have produced."""
    s = _settings(tmp_path)
    _zip(
        s.raw_dir / "BV_139440_HISTORY_SALES_WEEKLY_03282026_KW.zip",
        f"{SALES_WEEKLY_HDR}\n"
        f"{_sales_row('2026-03-28', amount=99.0)}\n"
        f"{_sales_row('2025-01-11')}\n",
    )
    _zip(
        s.raw_dir / "BV_139440_WEEKLY_SALES_TCIN_LOC_03282026_KW.zip",
        f"{SALES_WEEKLY_HDR}\n{_sales_row('2026-03-28', amount=19.99)}\n",
    )

    wh = Warehouse(s.db_path)
    try:
        r = await reingest_local_files(wh, s)
        assert r.files_loaded == 2, r
        _, rows = wh.execute_sql(
            "SELECT CAST(sales_date AS DATE), ROUND(SUM(sale_amount), 2) "
            "FROM sales_weekly GROUP BY 1 ORDER BY 1"
        )
        assert rows == [
            (date(2025, 1, 11), 19.99),  # HISTORY-only week survives
            (date(2026, 3, 28), 19.99),  # regular file replaced the HISTORY 99.0
        ]
    finally:
        wh.close()


# ---------- raw-dir eviction protection ----------


def test_raw_cap_never_evicts_unledgered_zips(tmp_path: Path) -> None:
    """An un-ingested zip may be the only copy of its data anywhere: the LRU
    cap must skip it (even while over budget) and only evict loaded zips."""
    raw = tmp_path / "raw"
    raw.mkdir()
    orphan = raw / "BV_139440_HISTORY_SALES_WEEKLY_01032026_KW.zip"
    orphan.write_bytes(b"o" * 4096)
    ledgered = raw / "BV_139440_WEEKLY_SALES_TCIN_LOC_04252026_KW.zip"
    ledgered.write_bytes(b"l" * 4096)
    # Make the orphan the OLDEST file — i.e. the first LRU victim.
    os.utime(orphan, (1_000, 1_000))
    os.utime(ledgered, (2_000, 2_000))

    _enforce_raw_dir_cap(raw, 1024, loaded_names={ledgered.name})

    assert orphan.exists(), "un-ingested zip must never be evicted"
    assert not ledgered.exists(), "loaded zip is fair game for LRU eviction"


# ---------- stale dimensional snapshots (Patch #10 — the location_attr incident) ----------

LOC_ATTR_OLD = "ALL_WKLY_LOC_ATTR_V0_0_05022026_KW.zip"
LOC_ATTR_NEW = "ALL_WKLY_LOC_ATTR_V0_0_07242026_KW.zip"


def _loc_attr_body(remodel: str) -> str:
    return (
        "LOCATION_NUMBER\tLOCATION_NAME\tLAST_REMODEL_DATE\n"
        f"1442\tSTORE A\t{remodel}\n"
    )


async def test_reingest_never_rolls_a_dimension_back(tmp_path: Path) -> None:
    """Replay of the July 2026 incident. Dimensional datasets are full-universe
    last-write-wins snapshots: only the newest file may load, in every mode."""
    s = _settings(tmp_path)
    _zip(s.raw_dir / LOC_ATTR_OLD, _loc_attr_body("2026-05-01"))
    _zip(s.raw_dir / LOC_ATTR_NEW, _loc_attr_body("2026-07-20"))

    wh = Warehouse(s.db_path)
    try:
        # Fresh warehouse, both zips unledgered: ONLY the newest loads.
        r1 = await reingest_local_files(wh, s)
        assert (r1.files_loaded, r1.files_skipped) == (1, 1), r1
        by_name = {o.file_name: o for o in r1.outcomes}
        assert by_name[LOC_ATTR_OLD].status == "skipped"
        assert "stale dimensional" in (by_name[LOC_ATTR_OLD].error or "")
        _, rows = wh.execute_sql(
            "SELECT MAX(CAST(last_remodel_date AS DATE)) FROM location_attr"
        )
        assert str(rows[0][0]) == "2026-07-20"

        # Incident shape: newest is ledgered, old zip is an unledgered orphan.
        # Pre-guard this loaded the old snapshot and rolled the dimension back.
        r2 = await reingest_local_files(wh, s)
        assert r2.files_loaded == 0, r2
        assert r2.files_skipped == 2  # new: already loaded; old: stale
        _, rows2 = wh.execute_sql(
            "SELECT MAX(CAST(last_remodel_date AS DATE)) FROM location_attr"
        )
        assert str(rows2[0][0]) == "2026-07-20"

        # Deliberate rebuild (only_unledgered=False): still only the newest.
        r3 = await reingest_local_files(wh, s, only_unledgered=False)
        assert (r3.files_loaded, r3.files_skipped) == (1, 1), r3
        _, led = wh.execute_sql(
            "SELECT file_name FROM _file_ledger WHERE dataset='location_attr' "
            "AND status='loaded'"
        )
        assert {r[0] for r in led} == {LOC_ATTR_NEW}
    finally:
        wh.close()


async def test_reingest_stale_guard_leaves_period_replace_datasets_alone(
    tmp_path: Path,
) -> None:
    """Only DIMENSIONAL datasets get the newest-only treatment — transactional
    period-replace files must all load (that's the whole recovery path)."""
    s = _settings(tmp_path)
    _zip(
        s.raw_dir / "BV_139440_HISTORY_SALES_WEEKLY_01032026_KW.zip",
        f"{SALES_WEEKLY_HDR}\n{_sales_row('2026-01-03')}\n",
    )
    _zip(
        s.raw_dir / "BV_139440_WEEKLY_SALES_TCIN_LOC_04252026_KW.zip",
        f"{SALES_WEEKLY_HDR}\n{_sales_row('2026-04-25')}\n",
    )
    wh = Warehouse(s.db_path)
    try:
        r = await reingest_local_files(wh, s)
        assert (r.files_loaded, r.files_skipped) == (2, 0), r
    finally:
        wh.close()


@respx.mock
async def test_sync_skips_stale_dimensional_before_download(tmp_path: Path) -> None:
    """Live-sync side of the guard: a stale dimensional file in the folder
    listing is skipped WITHOUT being downloaded (its content route is not even
    mocked — a download attempt would error the test)."""
    s = _settings(tmp_path)
    _mock_kiteworks(
        [
            {
                "id": "OLDDIM",
                "name": LOC_ATTR_OLD,
                "type": "f",
                "parentId": "F1",
                "size": 100,
                "fingerprint": "fp-old",
            },
            {
                "id": "NEWDIM",
                "name": LOC_ATTR_NEW,
                "type": "f",
                "parentId": "F1",
                "size": 100,
                "fingerprint": "fp-new",
            },
        ]
    )
    zb = tmp_path / "payload.zip"
    _zip(zb, _loc_attr_body("2026-07-20"))
    respx.get(f"{BASE}/rest/files/NEWDIM/content").mock(
        return_value=httpx.Response(200, content=zb.read_bytes())
    )
    zb.unlink()

    wh = Warehouse(s.db_path)
    try:
        async with httpx.AsyncClient() as http:
            auth = AuthManager(s, http, bundle=_bundle())
            client = KiteworksClient(s, auth, http)
            r = await sync_new_files(client, wh, s, triggered_by="test")
        assert (r.files_loaded, r.files_skipped) == (1, 1), r
        by_name = {o.file_name: o for o in r.outcomes}
        assert by_name[LOC_ATTR_OLD].status == "skipped"
        assert "stale dimensional" in (by_name[LOC_ATTR_OLD].error or "")
        _, rows = wh.execute_sql(
            "SELECT MAX(CAST(last_remodel_date AS DATE)) FROM location_attr"
        )
        assert str(rows[0][0]) == "2026-07-20"
    finally:
        wh.close()


async def test_dimensional_falls_back_when_newest_is_corrupt(tmp_path: Path) -> None:
    """Adversarial-review fix: staleness is judged against VERIFIED loads only.
    A corrupt newest file must not block the next-newest good snapshot (and a
    non-zip file must fail its own ledger row, not crash the batch)."""
    s = _settings(tmp_path)
    corrupt = s.raw_dir / "ALL_WKLY_LOC_ATTR_V0_0_07312026_KW.zip"
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_bytes(b"this is not a zip file")
    _zip(s.raw_dir / LOC_ATTR_NEW, _loc_attr_body("2026-07-20"))

    wh = Warehouse(s.db_path)
    try:
        r = await reingest_local_files(wh, s)
        assert r.files_failed == 1, r
        assert r.files_loaded == 1, r
        by_name = {o.file_name: o for o in r.outcomes}
        assert by_name[corrupt.name].status == "failed"
        assert by_name[LOC_ATTR_NEW].status == "loaded"
        _, rows = wh.execute_sql(
            "SELECT MAX(CAST(last_remodel_date AS DATE)) FROM location_attr"
        )
        assert str(rows[0][0]) == "2026-07-20"
        # The corrupt file is ledgered as failed, so the next run retries it
        # while the good snapshot stays loaded.
        _, led = wh.execute_sql(
            "SELECT file_name, status FROM _file_ledger ORDER BY file_name"
        )
        assert (corrupt.name, "failed") in led
    finally:
        wh.close()


@respx.mock
async def test_sync_dimensional_falls_back_when_newest_download_is_corrupt(
    tmp_path: Path,
) -> None:
    """Live-sync variant: the newest dimensional file downloads but fails to
    parse — the next-newest must then load instead of being pre-skipped."""
    s = _settings(tmp_path)
    newest = "ALL_WKLY_LOC_ATTR_V0_0_07312026_KW.zip"
    _mock_kiteworks(
        [
            {
                "id": "GOODDIM",
                "name": LOC_ATTR_NEW,
                "type": "f",
                "parentId": "F1",
                "size": 100,
                "fingerprint": "fp-good",
            },
            {
                "id": "BADDIM",
                "name": newest,
                "type": "f",
                "parentId": "F1",
                "size": 100,
                "fingerprint": "fp-bad",
            },
        ]
    )
    respx.get(f"{BASE}/rest/files/BADDIM/content").mock(
        return_value=httpx.Response(200, content=b"garbage, not a zip")
    )
    zb = tmp_path / "payload.zip"
    _zip(zb, _loc_attr_body("2026-07-20"))
    respx.get(f"{BASE}/rest/files/GOODDIM/content").mock(
        return_value=httpx.Response(200, content=zb.read_bytes())
    )
    zb.unlink()

    wh = Warehouse(s.db_path)
    try:
        async with httpx.AsyncClient() as http:
            auth = AuthManager(s, http, bundle=_bundle())
            client = KiteworksClient(s, auth, http)
            r = await sync_new_files(client, wh, s, triggered_by="test")
        assert (r.files_loaded, r.files_failed) == (1, 1), r
        by_name = {o.file_name: o for o in r.outcomes}
        assert by_name[newest].status == "failed"
        assert by_name[LOC_ATTR_NEW].status == "loaded"
        _, rows = wh.execute_sql(
            "SELECT MAX(CAST(last_remodel_date AS DATE)) FROM location_attr"
        )
        assert str(rows[0][0]) == "2026-07-20"
    finally:
        wh.close()
