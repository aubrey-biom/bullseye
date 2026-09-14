"""The ecomm team's DTC pacing targets, as a checked-in config the server reads.

WHERE THE NUMBERS COME FROM. The DTC ecomm team runs a Google Sheet, "2026 Daily
Pacing & Performance D2C | Biom", with one tab per month. Each tab has a header
row, one row per calendar day, and a summary block (Total / To Date / To Go /
Days Left / Daily Average Needed / weekly subtotals). The daily *forecast*
columns — Forecast (demand), Forecasted Spend, Forecasted NCs, Forecasted BRoAS,
Forecasted NC DMD / RoAS / AOV — are set once, when the month's tab is created,
and are the targets the team paces against. The *actual* columns are their own
pulls from Shopify and the ad platforms.

WHY A CONFIG FILE AND NOT A LIVE READ. The server holds exactly one credential,
the read-only BigQuery service account, and reads nothing but BigQuery. Reading
the sheet live would add a second credential, a second network dependency and a
second thing the health check must understand — for a series that changes once
a month. So the targets live in `config/dtc_pacing_targets.json`, regenerated
from an .xlsx export of the sheet by `scripts/refresh_pacing_targets.py`
(the same pattern as `config/pspw_goals.json` for the Target brief). The file
records which export it came from and when, and the pacing tool reports that
provenance with every response, so a stale file is visible rather than silent.

ONLY THE FORECAST COLUMNS ARE READ. The sheet also carries the team's own
actuals (Actual DMD, Actual Spend, Total NCs, LY Total DMD), pulled by hand from
Shopify and the ad platforms. Those are deliberately not in FIELD_MAP: every
actual the tools report comes from BigQuery, and the sheet is the source of
targets and nothing else. (An earlier revision kept them for a per-day
reconciliation display; it mostly flagged the sheet's not-yet-filled rows and
invited confusion about which number was the real one.)

This module is the read side. It has no BigQuery dependency and is safe to
import anywhere.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

#: Repo-relative default. Overridable per call (`load_targets(path)`; the tests do).
DEFAULT_TARGETS_PATH = Path(__file__).resolve().parents[2] / "config" / "dtc_pacing_targets.json"

REFRESH_COMMAND = "uv run python scripts/refresh_pacing_targets.py <export.xlsx>"

#: The sheet columns each target field is read from. The refresh script matches
#: on whitespace-normalised header text, so a renamed column fails loudly
#: (the field comes back absent) instead of silently reading the wrong one.
FIELD_MAP: dict[str, str] = {
    "forecast_demand": "Forecast",
    "forecast_spend": "Forecasted Spend",
    "forecast_new_customers": "Forecasted NCs",
    "forecast_broas": "Forecasted BRoAS",
    "forecast_nc_demand": "Forecasted NC DMD",
    "forecast_nc_roas": "Forecasted NC RoAS",
    "forecast_nc_aov": "Forecasted NC AOV",
}

TARGET_FIELDS: tuple[str, ...] = tuple(FIELD_MAP)

#: The three series the pacing tool paces against; the sheet's Total row states
#: a month figure for each, which the refresh script records as `totals`.
PACED_FIELDS: tuple[str, ...] = ("forecast_demand", "forecast_spend", "forecast_new_customers")


class TargetsUnavailable(RuntimeError):
    """The targets file is missing or malformed. Carries the remediation."""


@dataclass(frozen=True)
class DayTarget:
    values: dict[str, float | None] = field(default_factory=dict)

    def get(self, name: str) -> float | None:
        return self.values.get(name)


@dataclass(frozen=True)
class MonthTargets:
    month: str
    """`YYYY-MM`."""
    tab: str
    """The sheet tab the month was read from, e.g. `September 2026`."""
    days: dict[date, DayTarget]
    totals: dict[str, float | None]
    """The sheet's own Total row for the PACED_FIELDS (None when the tab lacked it)."""

    @property
    def first_day(self) -> date:
        return min(self.days)

    @property
    def last_day(self) -> date:
        return max(self.days)

    def series_sum(
        self, name: str, start: date | None = None, end: date | None = None
    ) -> float | None:
        """Sum of one daily target series over [start, end] (inclusive, defaults to the month).

        None when NO day in the window carries the series — an absent forecast
        column is "no target", never a $0 target.
        """
        total = 0.0
        seen = False
        for d, t in self.days.items():
            if (start is None or d >= start) and (end is None or d <= end):
                v = t.get(name)
                if v is not None:
                    total += v
                    seen = True
        return total if seen else None

    def series_mean(
        self, name: str, start: date | None = None, end: date | None = None
    ) -> float | None:
        """Mean of one daily target series over the window (the sheet's rate targets —
        NC RoAS, BRoAS — are constants repeated per day). None when absent."""
        vals = [
            v
            for d, t in self.days.items()
            if (start is None or d >= start) and (end is None or d <= end)
            for v in [t.get(name)]
            if v is not None
        ]
        return sum(vals) / len(vals) if vals else None

    def month_total(self, name: str) -> float | None:
        """The sheet's stated Total for a paced field, else the sum of its days."""
        stated = self.totals.get(name)
        if stated is not None:
            return stated
        if any(t.get(name) is not None for t in self.days.values()):
            return self.series_sum(name)
        return None


@dataclass(frozen=True)
class PacingTargets:
    source: dict[str, Any]
    months: dict[str, MonthTargets]
    path: Path | None = None

    def month(self, ym: str) -> MonthTargets | None:
        return self.months.get(ym)

    @property
    def available_months(self) -> list[str]:
        return sorted(self.months)


def coerce_number(v: Any) -> float | None:
    """A sheet or JSON cell as a float: numbers pass, numeric strings ("$1,234", "12%")
    are parsed, everything else ("-", "#REF!", blanks, bools) is None. Shared by the
    loader and the refresh script so the two can never disagree on a cell."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int | float):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(",", "").replace("$", "").replace("%", "")
        try:
            return float(s)
        except ValueError:
            return None
    return None


def parse_targets(payload: dict[str, Any], *, path: Path | None = None) -> PacingTargets:
    """Build `PacingTargets` from the JSON document. Raises TargetsUnavailable on shape errors."""
    version = payload.get("schema_version")
    if version != SCHEMA_VERSION:
        raise TargetsUnavailable(
            f"pacing targets schema_version {version!r} is not {SCHEMA_VERSION}; "
            f"regenerate with `{REFRESH_COMMAND}`"
        )
    raw_months = payload.get("months")
    if not isinstance(raw_months, dict):
        raise TargetsUnavailable("pacing targets: `months` must be an object keyed by YYYY-MM")
    months: dict[str, MonthTargets] = {}
    for ym, m in raw_months.items():
        if not isinstance(m, dict) or not isinstance(m.get("days"), dict):
            raise TargetsUnavailable(f"pacing targets: month {ym!r} has no `days` object")
        days: dict[date, DayTarget] = {}
        for ds, vals in m["days"].items():
            try:
                d = date.fromisoformat(ds)
            except ValueError as e:
                raise TargetsUnavailable(f"pacing targets: {ym}: bad day key {ds!r}") from e
            if d.strftime("%Y-%m") != ym:
                raise TargetsUnavailable(f"pacing targets: {ym}: day {ds} is outside the month")
            if not isinstance(vals, dict):
                raise TargetsUnavailable(f"pacing targets: {ym}/{ds}: values must be an object")
            days[d] = DayTarget({k: coerce_number(v) for k, v in vals.items()})
        if not days:
            raise TargetsUnavailable(f"pacing targets: month {ym!r} has no days")
        totals_raw = m.get("totals")
        if totals_raw is None:
            totals_raw = {}
        if not isinstance(totals_raw, dict):
            raise TargetsUnavailable(f"pacing targets: month {ym!r}: `totals` must be an object")
        totals = {k: coerce_number(totals_raw.get(k)) for k in PACED_FIELDS}
        months[ym] = MonthTargets(month=ym, tab=str(m.get("tab", "")), days=days, totals=totals)
    source = payload.get("source") or {}
    if not isinstance(source, dict):
        raise TargetsUnavailable("pacing targets: `source` must be an object")
    return PacingTargets(source=source, months=months, path=path)


# Parsed file keyed by (path, mtime_ns): the file changes once a month, the tool
# is called far more often, and a refreshed file is still picked up without a
# restart because the mtime moves.
_CACHE: dict[Path, tuple[int, PacingTargets]] = {}


def load_targets(path: Path | str | None = None) -> PacingTargets:
    """Read and parse the targets file. Raises TargetsUnavailable if absent or malformed."""
    p = Path(path) if path is not None else DEFAULT_TARGETS_PATH
    try:
        mtime = p.stat().st_mtime_ns
    except FileNotFoundError:
        raise TargetsUnavailable(
            f"pacing targets file not found at {p}; export the pacing sheet to .xlsx and run "
            f"`{REFRESH_COMMAND}`"
        ) from None
    cached = _CACHE.get(p)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        payload = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise TargetsUnavailable(f"pacing targets at {p} could not be read: {e}") from e
    if not isinstance(payload, dict):
        raise TargetsUnavailable(f"pacing targets at {p}: top level must be an object")
    parsed = parse_targets(payload, path=p)
    _CACHE[p] = (mtime, parsed)
    return parsed
