"""
MCP server for the SolisCloud (Ginlong Solis) PV monitoring API.

Exposes plants, inverters, collectors, EPM devices, weather stations, meters and
alarms as MCP tools so an LLM client (Claude Desktop, Claude Code, etc.) can
query your solar installation.

Built against the SolisCloud Platform API Document V2.0.3 (revised 24 Dec 2025):
https://usservice.solisinverters.com/support/solutions/articles/73000591225-soliscloud-api-guide

Several parameters that document marks Required=Y are optional in practice
(`money` throughout, `timeZone` on the station month/year endpoints) -- see the
README for the behaviours measured against the live API.

Setup
-----
1. Request API access from SolisCloud: log in to https://www.soliscloud.com,
   go to Service -> API Management, activate it, and note the KeyId, KeySecret
   and API base URL you're given.
2. Set the environment variables below (or copy .env.example to .env).
3. Serve it over HTTP (`uvicorn server:app`) and point your MCP client at /mcp.

Environment variables
----------------------
SOLIS_KEY_ID            (required) API key ID issued by SolisCloud
SOLIS_KEY_SECRET        (required) API key secret issued by SolisCloud
SOLIS_API_BASE_URL      (optional) defaults to https://www.soliscloud.com:13333
SOLIS_DEFAULT_TIMEZONE  (optional) UTC offset, e.g. 10 or 9.5. Overrides the
                        per-device timezone lookup. Normally unnecessary --
                        the offset is read from the device record.
SOLIS_TOOLSETS          (optional) comma-separated tool groups to expose.
                        Defaults to "core". Groups: core, fleet, collector,
                        warranty, epm, weather, meter, or "all" for every
                        read group. See TOOLSETS below.
SOLIS_ENABLE_WRITES     (optional) set to 1/true/yes to also expose the five
                        tools that modify the installation (create_station,
                        update_station, create_station_with_collector,
                        bind_inverters, unbind_collector). Off by default, and
                        the only way to enable the "write" group.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any, Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from soliscloud_client import DEFAULT_BASE_URL, SolisCloudAPIError, SolisCloudClient

# Load variables from a .env file in this directory (if present) into the
# environment. Explicit environment variables (e.g. set in an MCP client's
# "env" config block) still take priority over .env.
# load_dotenv()

mcp = FastMCP(
    "soliscloud",
    instructions=(
        "SolisCloud PV monitoring.\n"
        "1. All data is refreshed server-side every 5 minutes. Repeating a call "
        "sooner than that returns identical values. Past days, months and years "
        "are final.\n"
        "2. Endpoints are rate-limited to 2 requests/sec.\n"
        "3. Numbers are already scaled to real values. Each one's unit is in its "
        "companion *Str or *Unit field (e.g. pac 4.64 with pacStr 'kW'). Units "
        "are not fixed across responses (kWh vs MWh), so read the unit rather "
        "than assuming one.\n"
        "4. Ids are optional on most tools when the account has a single plant "
        "and inverter."
    ),
)

# Groups a tool can belong to, selected with SOLIS_TOOLSETS. Only "core" is on
# by default to keep the tool list (sent with every request) small.
TOOLSETS = {
    "core": "plant and inverter status, energy history, alarms",
    "fleet": "batch and list tools for accounts with several plants",
    "collector": "datalogger status and signal strength",
    "warranty": "inverter warranty dates",
    "epm": "Export Power Manager devices",
    "weather": "meteorological instruments",
    "meter": "electricity meters",
    "write": "tools that modify the installation (SOLIS_ENABLE_WRITES only)",
}

READ = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True)

_client: Optional[SolisCloudClient] = None

# uvicorn configures this logger before importing the app, so messages logged
# here appear in the container log alongside uvicorn's own.
log = logging.getLogger("uvicorn.error")


def credential_problems() -> list[str]:
    """Describe what is wrong with the SolisCloud settings; empty when usable."""
    problems = []
    unresolved = []
    for var in ("SOLIS_KEY_ID", "SOLIS_KEY_SECRET", "SOLIS_API_BASE_URL"):
        value = os.environ.get(var, "").strip()
        if not value:
            # The base URL has a default, so only the keys are required.
            if var != "SOLIS_API_BASE_URL":
                problems.append(f"{var} is not set or empty")
        elif value.startswith("op://"):
            unresolved.append(var)
        elif var == "SOLIS_API_BASE_URL" and not re.match(r"https?://", value):
            problems.append(f"SOLIS_API_BASE_URL must start with https:// (got {value!r})")
    if unresolved:
        problems.append(
            f"unresolved 1Password op:// reference in {', '.join(unresolved)}; "
            "start the server through `op run --env-file=.env -- ...`"
        )
    return problems


def get_client() -> SolisCloudClient:
    global _client
    if _client is None:
        problems = credential_problems()
        if problems:
            raise ToolError(
                "SolisCloud credentials are not configured: "
                + "; ".join(problems)
                + ". Get the KeyId, KeySecret and API URL from SolisCloud > "
                "Service > API Management."
            )
        _client = SolisCloudClient(
            os.environ["SOLIS_KEY_ID"].strip(),
            os.environ["SOLIS_KEY_SECRET"].strip(),
            base_url=os.environ.get("SOLIS_API_BASE_URL", "").strip() or DEFAULT_BASE_URL,
        )
    return _client


async def _call(path: str, params: dict[str, Any] | None = None) -> Any:
    """Call the API, surfacing failures as MCP tool errors (isError=true)."""
    try:
        try:
            return await get_client().call(path, params)
        except SolisCloudAPIError as exc:
            code = f" {exc.code}" if exc.code else ""
            hint = ""
            if exc.status_code in (401, 403):
                hint = " (SolisCloud rejected the request signature: check SOLIS_KEY_ID and SOLIS_KEY_SECRET)"
            raise ToolError(f"SolisCloud API error{code}: {exc}{hint}") from exc
        except OSError as exc:  # URLError and socket timeouts both subclass OSError
            raise ToolError(f"Could not reach SolisCloud: {exc}") from exc
    except ToolError as exc:
        # The HTTP response is still 200, so this line is the only trace of the
        # failure in the server log.
        log.warning("%s failed: %s", path, exc)
        raise


# ---------------------------------------------------------------------------
# Response shaping
#
# Raw payloads are large (inverterDetail has ~780 keys, one inverterDay call is
# ~470 KB) and every number arrives unscaled next to a *Pec factor. Tools scale
# values here, drop empty fields, and by default keep only the fields worth
# reading. Measured against the live API, value * *Pec in the *Str unit holds
# on every endpoint (pac 4640 * 0.001 = 4.64 kW; energy 6594 * 0.001 = 6.594
# MWh), so the factor can be applied unconditionally.
# ---------------------------------------------------------------------------


def _scale(row: dict[str, Any]) -> dict[str, Any]:
    """Multiply each field by its *Pec factor and drop the factor."""
    out = dict(row)
    for key, pec in row.items():
        if not key.endswith("Pec"):
            continue
        base = key[:-3]
        value = out.get(base)
        try:
            factor = float(pec)
        except (TypeError, ValueError):
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if factor != 1:
                out[base] = round(value * factor, 6)
        elif base in out:
            continue  # leave the factor alongside a value we couldn't scale
        del out[key]
    return out


def _compact(value: Any) -> Any:
    """Recursively drop None, empty strings and empty containers."""
    if isinstance(value, dict):
        out = {k: _compact(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        return [_compact(v) for v in value]
    return value


def _record(row: Any, fields: Iterable[str] | None = None) -> Any:
    if not isinstance(row, dict):
        return row
    row = _scale(row)
    if fields is not None:
        allowed = {name for f in fields for name in (f, f + "Str", f + "Unit")}
        row = {k: v for k, v in row.items() if k in allowed}
    return _compact(row)


def _shape(data: Any, fields: Iterable[str] | None = None) -> Any:
    """Scale and trim a response: a record, a list of records, or a page."""
    if fields is not None:
        fields = list(fields)
    if isinstance(data, list):
        return [_record(r, fields) for r in data]
    if isinstance(data, dict):
        page = data.get("page") if isinstance(data.get("page"), dict) else data
        if isinstance(page.get("records"), list):
            return {
                "total": page.get("total"),
                "records": [_record(r, fields) for r in page["records"]],
            }
        return _record(data, fields)
    return data


def _parse_fields(fields: str, default: Iterable[str]) -> list[str] | None:
    """Turn a comma-separated `fields` argument into a field list (None = all)."""
    if fields.strip().lower() == "all":
        return None
    chosen = [f.strip() for f in fields.split(",") if f.strip()]
    return chosen or list(default)


def _local_time(ms: Any, zone: Any = None) -> str | None:
    """Render an epoch-millisecond timestamp in a zone (IANA name or UTC offset).

    Prefer the record's timeZoneStandardId: its timeZone is the standard
    offset and ignores daylight saving.
    """
    try:
        if isinstance(zone, str):
            tz = ZoneInfo(zone)
        else:
            tz = dt_timezone(timedelta(hours=float(zone or 0)))
        moment = datetime.fromtimestamp(int(ms) / 1000, tz)
    except (TypeError, ValueError, OverflowError, ZoneInfoNotFoundError):
        return None
    return moment.isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Id and timezone resolution
#
# Measured against the live API, the timeZone request parameter behaves
# inconsistently across endpoints (tests/test_live.py re-checks inverterDay):
#
#   inverterDay    honours it -- sending 8 vs 10 shifts every timestamp by 2h.
#   inverterMonth  ignores it -- byte-identical responses for 8 vs 10.
#   stationDay     ignores it -- already returns the plant's local time.
#   stationMonth   ignores it -- byte-identical responses for 8 vs 10.
#
# So only inverterDay (and collector/day, which also requires it) resolves the
# offset from the device record, at the cost of one cached extra request. The
# endpoints that ignore the parameter never send it.
#
# The `timeZone` field inside response rows is not a reliable echo -- inverterDay
# rows report 8 regardless of what was sent, inverterMonth rows report the
# device's real 10.0, station rows report 8.
#
# SOLIS_DEFAULT_TIMEZONE overrides the lookup entirely.
# ---------------------------------------------------------------------------

_tz_cache: dict[str, float | str] = {}
_only_ids: dict[str, str] = {}


def _tz_number(value: float) -> Any:
    """Send 10 rather than 10.0, but preserve half-hour zones like 9.5."""
    return int(value) if float(value).is_integer() else float(value)


def _offset_on(zone: float | str, day: str | None) -> Any:
    """UTC offset in hours for a zone on a YYYY-MM-DD date (today if None).

    The device's timeZone is its standard offset and ignores daylight saving:
    measured on a Canberra inverter, sending 10 for a date in AEDT puts every
    inverterDay timestamp an hour early, while 11 lines up with stationDay.
    So when the device record names an IANA zone, the offset is taken from
    that zone at local noon on the requested date.
    """
    if isinstance(zone, str):
        tz = ZoneInfo(zone)
        when = datetime.now(tz)
        if day:
            when = datetime.strptime(day, "%Y-%m-%d").replace(hour=12, tzinfo=tz)
        return _tz_number(when.utcoffset().total_seconds() / 3600)
    return _tz_number(zone)


async def _resolve_timezone(kind: str, ident: str, day: str | None = None) -> Any:
    """Look up a station's, inverter's or collector's UTC offset, with caching.

    Args:
        kind: "station", "inverter" or "collector".
        ident: The station id, an inverter id/sn, or a collector sn.
        day: YYYY-MM-DD date the offset is needed for, so daylight saving is
            applied for that date. Defaults to today.
    """
    override = os.environ.get("SOLIS_DEFAULT_TIMEZONE")
    if override:
        return _tz_number(float(override))

    cache_key = f"{kind}:{ident}"
    if cache_key in _tz_cache:
        return _offset_on(_tz_cache[cache_key], day)

    try:
        if kind == "station":
            detail = await _call("/v1/api/stationDetail", {"id": ident})
        elif kind == "collector":
            detail = await _call("/v1/api/collectorDetail", {"sn": ident})
        else:
            # inverterDetail accepts id or sn; ids here are long numeric strings.
            key = "id" if ident.isdigit() else "sn"
            detail = await _call("/v1/api/inverterDetail", {key: ident})
    except ToolError:
        detail = None

    detail = detail if isinstance(detail, dict) else {}
    zone: float | str | None = None
    try:
        ZoneInfo(detail.get("timeZoneStandardId") or "")
        zone = detail["timeZoneStandardId"]
    except (ValueError, ZoneInfoNotFoundError):
        if detail.get("timeZone") is not None:
            zone = float(detail["timeZone"])
    if zone is None:
        # Lookup failed (error response, or field absent). Fall back to the
        # API's own documented example value rather than failing the call.
        return 8

    _tz_cache[cache_key] = zone
    return _offset_on(zone, day)


async def _only_id(target: Literal["station", "inverter"]) -> str:
    """Return the account's only plant or inverter id, for omitted id arguments."""
    if target in _only_ids:
        return _only_ids[target]
    path = "/v1/api/userStationList" if target == "station" else "/v1/api/inverterList"
    records = _shape(await _call(path, {"pageNo": 1, "pageSize": 10})).get("records", [])
    if len(records) != 1:
        options = ", ".join(
            f"{r.get('id')} ({r.get('stationName') or r.get('sn')})" for r in records
        )
        raise ToolError(
            f"The account has {len(records)} {target}s, so an id is required. "
            f"Options: {options or 'none'}"
        )
    _only_ids[target] = str(records[0]["id"])
    return _only_ids[target]


# ---------------------------------------------------------------------------
# Core: plant, inverter, energy, alarms
# ---------------------------------------------------------------------------

STATION_FIELDS = (
    "id", "stationName", "state", "capacity", "power", "porwerPercent",
    "dayEnergy", "monthEnergy", "yearEnergy", "allEnergy", "fullHour",
    "dayInCome", "monthInCome", "yearInCome", "allInCome", "money", "price",
    "inverterCount", "alarmLevel", "timeZone", "timeZoneName", "sr", "ss",
    "condTxtD", "tmpMin", "tmpMax", "dip", "azimuth", "generateDays",
)

INVERTER_FIELDS = (
    "id", "sn", "stationId", "stationName", "machine", "state", "currentState",
    "faultCodeDesc", "alarmState", "pac", "power", "eToday", "eMonth", "eYear",
    "eTotal", "fac", "powerFactor", "inverterTemperature", "collectorsn",
    "collectorState", "timeZone",
)

# Per-string DC and per-phase AC readings; kept when non-zero.
_STRING_FIELD = re.compile(r"(uPv|iPv|pow|uAc|iAc)\d+")


@mcp.tool(tags={"core"}, annotations=READ)
async def get_station(station_id: str = "", verbose: bool = False) -> Any:
    """Live status and energy totals for a plant.

    Returns power, today/month/year/lifetime energy and income, capacity,
    timezone and the day's weather. state: 1=online, 2=offline, 3=alarm.

    Args:
        station_id: Plant ID. Optional if the account has one plant.
        verbose: Return every field (~400) instead of the summary.
    """
    station_id = station_id or await _only_id("station")
    raw = await _call("/v1/api/stationDetail", {"id": station_id})
    if not isinstance(raw, dict):
        raise ToolError(f"No plant found with id {station_id}")
    if verbose:
        return _shape(raw)
    out = _shape(raw, STATION_FIELDS)
    out["updated"] = _local_time(
        raw.get("dataTimestamp"), raw.get("timeZoneStandardId") or raw.get("timeZone")
    )
    return out


@mcp.tool(tags={"core"}, annotations=READ)
async def get_inverter(
    inverter_id: str = "", inverter_sn: str = "", verbose: bool = False
) -> Any:
    """Live readings for an inverter: AC power, energy counters, per-string
    DC voltage/current/power, grid voltage/frequency, temperature, fault state.

    state: 1=online, 2=offline, 3=alarm.

    Args:
        inverter_id: Inverter ID. Optional if the account has one inverter.
        inverter_sn: Serial number, as an alternative to inverter_id.
        verbose: Return every field (~800) instead of the summary.
    """
    if inverter_sn:
        params = {"sn": inverter_sn}
    else:
        params = {"id": inverter_id or await _only_id("inverter")}
    raw = await _call("/v1/api/inverterDetail", params)
    if not isinstance(raw, dict):
        raise ToolError(f"No inverter found for {params}")
    if verbose:
        return _shape(raw)
    strings = [k for k, v in raw.items() if _STRING_FIELD.fullmatch(k) and v]
    out = _shape(raw, (*INVERTER_FIELDS, *strings))
    out["updated"] = _local_time(
        raw.get("dataTimestamp"), raw.get("timeZoneStandardId") or raw.get("timeZone")
    )
    return out


_ENERGY_PATHS = {
    ("station", "day"): ("/v1/api/stationDay", "time"),
    ("station", "month"): ("/v1/api/stationMonth", "month"),
    ("station", "year"): ("/v1/api/stationYear", "year"),
    ("station", "lifetime"): ("/v1/api/stationAll", None),
    ("inverter", "day"): ("/v1/api/inverterDay", "time"),
    ("inverter", "month"): ("/v1/api/inverterMonth", "month"),
    ("inverter", "year"): ("/v1/api/inverterYear", "year"),
    ("inverter", "lifetime"): ("/v1/api/inverterAll", None),
}

_DATE_FORMATS = {
    "day": (r"\d{4}-\d{2}-\d{2}", "YYYY-MM-DD"),
    "month": (r"\d{4}-\d{2}", "YYYY-MM"),
    "year": (r"\d{4}", "YYYY"),
}

_ENERGY_FIELDS = {
    ("station", "day"): ("timeStr", "power"),
    ("inverter", "day"): ("timeStr", "pac", "eToday"),
}
_PERIOD_FIELDS = ("dateStr", "energy", "fullHour")


@mcp.tool(tags={"core"}, annotations=READ)
async def get_energy(
    period: Literal["day", "month", "year", "lifetime"],
    date: str = "",
    target: Literal["station", "inverter"] = "station",
    target_id: str = "",
    fields: str = "",
) -> Any:
    """Generation history for a plant or inverter.

    - day: 5-minute power series (date YYYY-MM-DD). Power is instantaneous;
      summing it does not give energy. For the day's total use the inverter
      series' final eToday, or the matching row of the month view.
    - month: one energy row per day (date YYYY-MM).
    - year: one energy row per month (date YYYY).
    - lifetime: one energy row per year (no date).

    fullHour is energy divided by capacity (peak-sun hours).

    Args:
        period: day, month, year or lifetime.
        date: Required for day/month/year, in the format above.
        target: station (default) or inverter.
        target_id: Plant or inverter ID. Optional with a single plant/inverter.
        fields: Comma-separated fields to return per row, or "all". Defaults
            to timeStr,power (station day), timeStr,pac,eToday (inverter day)
            or dateStr,energy,fullHour. Other inverter-day fields include
            uPv1, iPv1, uAc1, iAc1, fac and inverterTemperature.
    """
    path, date_param = _ENERGY_PATHS[(target, period)]
    if date_param:
        pattern, label = _DATE_FORMATS[period]
        if not re.fullmatch(pattern, date):
            raise ToolError(f"period={period} needs date in {label} format, got {date!r}")
    target_id = target_id or await _only_id(target)
    params: dict[str, Any] = {"id": target_id}
    if date_param:
        params[date_param] = date
    if (target, period) == ("inverter", "day"):
        params["timeZone"] = await _resolve_timezone("inverter", target_id, date)

    default = _ENERGY_FIELDS.get((target, period), _PERIOD_FIELDS)
    chosen = _parse_fields(fields, default)
    if chosen is not None:
        chosen += ["timeStr", "dateStr"]  # always keep the row's time label
    return _shape(await _call(path, params), chosen)


ALARM_FIELDS = (
    "alarmDeviceSn", "alarmMsg", "alarmCode", "alarmLevel", "state", "advice",
    "alarmBeginTime", "alarmEndTime",
)


@mcp.tool(tags={"core"}, annotations=READ)
async def list_alarms(
    station_id: str = "",
    page_no: int = 1,
    page_size: int = 20,
    alarm_start_date: str = "",
    alarm_end_date: str = "",
) -> Any:
    """List device alarms/faults, newest first. Times are UTC.

    state: 0=pending, 1=handled, 2=recovered. A new fault takes up to 5
    minutes to appear.

    Args:
        station_id: Optional plant ID to filter by.
        page_no: 1-based page number.
        page_size: Number of results per page.
        alarm_start_date: Optional start date filter, YYYY-MM-DD.
        alarm_end_date: Optional end date filter, YYYY-MM-DD.
    """
    params: dict[str, Any] = {"pageNo": page_no, "pageSize": page_size}
    if station_id:
        params["stationId"] = station_id
    if alarm_start_date:
        params["alarmBeginTime"] = alarm_start_date
    if alarm_end_date:
        params["alarmEndTime"] = alarm_end_date
    out = _shape(await _call("/v1/api/alarmList", params), ALARM_FIELDS)
    for alarm in out.get("records", []):
        for key in ("alarmBeginTime", "alarmEndTime"):
            if key in alarm:
                alarm[key] = _local_time(alarm[key])
    return out


# ---------------------------------------------------------------------------
# Fleet: list and batch tools for accounts with several plants
# ---------------------------------------------------------------------------


@mcp.tool(tags={"fleet"}, annotations=READ)
async def list_stations(page_no: int = 1, page_size: int = 20) -> Any:
    """List all PV plants under the account, with live power and status.

    Args:
        page_no: 1-based page number.
        page_size: Number of results per page (max 100).
    """
    return _shape(
        await _call("/v1/api/userStationList", {"pageNo": page_no, "pageSize": page_size})
    )


@mcp.tool(tags={"fleet"}, annotations=READ)
async def list_inverters(station_id: str = "", page_no: int = 1, page_size: int = 20) -> Any:
    """List inverters under the account, optionally filtered to one plant.

    Args:
        station_id: Optional plant ID to filter by.
        page_no: 1-based page number.
        page_size: Number of results per page.
    """
    params: dict[str, Any] = {"pageNo": page_no, "pageSize": page_size}
    if station_id:
        params["stationId"] = station_id
    return _shape(await _call("/v1/api/inverterList", params))


@mcp.tool(tags={"fleet"}, annotations=READ)
async def list_station_details(
    page_no: int = 1,
    page_size: int = 20,
    id_list: list[str] | None = None,
    station_type: int | None = None,
) -> Any:
    """Full detail records for many plants in one call (one request, not N).

    Args:
        page_no: 1-based page number.
        page_size: Results per page (max 100).
        id_list: Optional list of plant IDs. Omit for all plants.
        station_type: Set to 3 for guest accounts; otherwise omit.
    """
    params: dict[str, Any] = {"pageNo": page_no, "pageSize": page_size}
    if id_list:
        params["idList"] = id_list
    if station_type is not None:
        params["stationType"] = station_type
    return _shape(await _call("/v1/api/stationDetailList", params))


@mcp.tool(tags={"fleet"}, annotations=READ)
async def list_inverter_details(
    page_no: int = 1,
    page_size: int = 20,
    sn_list: list[str] | None = None,
    station_type: int | None = None,
) -> Any:
    """Full detail records for many inverters in one call (one request, not N).

    Args:
        page_no: 1-based page number.
        page_size: Results per page (max 100).
        sn_list: Optional list of inverter serial numbers. Omit for all.
        station_type: Set to 3 for guest accounts; otherwise omit.
    """
    params: dict[str, Any] = {"pageNo": page_no, "pageSize": page_size}
    if sn_list:
        params["snList"] = sn_list
    if station_type is not None:
        params["stationType"] = station_type
    return _shape(await _call("/v1/api/inverterDetailList", params))


@mcp.tool(tags={"fleet"}, annotations=READ)
async def list_station_energy(
    period: Literal["day", "month", "year"],
    date: str,
    page_no: int = 1,
    page_size: int = 20,
    id_list: list[str] | None = None,
    nmi_code: str = "",
) -> Any:
    """Energy totals for many plants in one call, to compare plants.

    - day: one row per plant with that day's energy (date YYYY-MM-DD).
    - month: daily totals for a month per plant (date YYYY-MM).
    - year: yearly totals per plant (date YYYY).

    Args:
        period: day, month or year.
        date: Date in the format above.
        page_no: 1-based page number.
        page_size: Results per page (max 100).
        id_list: Optional list of plant IDs. Omit for all plants.
        nmi_code: Optional NMI code to filter by (month and year only).
    """
    pattern, label = _DATE_FORMATS[period]
    if not re.fullmatch(pattern, date):
        raise ToolError(f"period={period} needs date in {label} format, got {date!r}")
    path = {
        "day": "/v1/api/stationDayEnergyList",
        "month": "/v1/api/stationMonthEnergyList",
        "year": "/v1/api/stationYearEnergyList",
    }[period]
    params: dict[str, Any] = {"pageNo": page_no, "pageSize": page_size, "time": date}
    if id_list:
        params["idList"] = id_list
    if nmi_code and period != "day":
        params["nmiCode"] = nmi_code
    return _shape(await _call(path, params))


# ---------------------------------------------------------------------------
# Collectors (dataloggers)
# ---------------------------------------------------------------------------


@mcp.tool(tags={"collector"}, annotations=READ)
async def list_collectors(
    page_no: int = 1, page_size: int = 20, station_id: str = "", nmi_code: str = ""
) -> Any:
    """List data collectors (dataloggers), the comms devices that upload data.

    state: 1=online, 2=offline. An offline collector explains stale readings
    everywhere else, so check it first when a plant stops reporting.

    Args:
        page_no: 1-based page number.
        page_size: Results per page (max 100).
        station_id: Optional plant ID to filter by.
        nmi_code: Optional NMI code to filter by.
    """
    params: dict[str, Any] = {"pageNo": page_no, "pageSize": page_size}
    if station_id:
        params["stationId"] = station_id
    if nmi_code:
        params["nmiCode"] = nmi_code
    return _shape(await _call("/v1/api/collectorList", params))


@mcp.tool(tags={"collector"}, annotations=READ)
async def get_collector_detail(collector_id: str = "", collector_sn: str = "") -> Any:
    """Details for one collector: signal strength, uptime, firmware, SIM data.

    Args:
        collector_id: The SolisCloud collector ID.
        collector_sn: The collector's serial number (alternative to the ID).
    """
    if not collector_id and not collector_sn:
        raise ToolError("Provide collector_id or collector_sn")
    params: dict[str, Any] = {}
    if collector_id:
        params["id"] = collector_id
    if collector_sn:
        params["sn"] = collector_sn
    return _shape(await _call("/v1/api/collectorDetail", params))


@mcp.tool(tags={"collector"}, annotations=READ)
async def get_collector_day(collector_sn: str, date: str) -> Any:
    """A collector's signal-strength series for one day.

    Returns rssi, rssiLevel and signal percentage sampled through the day.
    Useful for correlating data gaps with poor connectivity.

    Args:
        collector_sn: The collector's serial number.
        date: Date in YYYY-MM-DD format.
    """
    timezone = await _resolve_timezone("collector", collector_sn, date)
    return _shape(
        await _call(
            "/v1/api/collector/day",
            {"sn": collector_sn, "time": date, "timeZone": timezone},
        )
    )


# ---------------------------------------------------------------------------
# Warranty
# ---------------------------------------------------------------------------


@mcp.tool(tags={"warranty"}, annotations=READ)
async def get_inverter_warranty(
    page_no: int = 1, page_size: int = 20, sn: str = ""
) -> Any:
    """Warranty start/end dates and status for one or more inverters.

    shelfState: 0=under warranty, 1=out of warranty.

    Args:
        page_no: 1-based page number.
        page_size: Results per page (max 100).
        sn: Optional inverter serial number. For several, comma-separate them
            (max 1000 per call). Omit for all inverters on the account.
    """
    params: dict[str, Any] = {"pageNo": page_no, "pageSize": page_size}
    if sn:
        params["sn"] = sn
    return _shape(await _call("/v1/api/inverter/shelfTime", params))


# ---------------------------------------------------------------------------
# EPM (Export Power Manager)
# ---------------------------------------------------------------------------


@mcp.tool(tags={"epm"}, annotations=READ)
async def list_epms(
    page_no: int = 1, page_size: int = 20, station_id: str = "", nmi_code: str = ""
) -> Any:
    """List EPM (Export Power Manager) devices under the account.

    An EPM limits export to the grid. Records carry total grid power plus
    lifetime purchased/sold energy.

    Args:
        page_no: 1-based page number.
        page_size: Results per page (max 100).
        station_id: Optional plant ID to filter by.
        nmi_code: Optional NMI code to filter by.
    """
    params: dict[str, Any] = {"pageNo": page_no, "pageSize": page_size}
    if station_id:
        params["stationId"] = station_id
    if nmi_code:
        params["nmiCode"] = nmi_code
    return _shape(await _call("/v1/api/epmList", params))


@mcp.tool(tags={"epm"}, annotations=READ)
async def get_epm_detail(epm_sn: str) -> Any:
    """Full details for one EPM: per-phase V/I/P, power limit, CT ratio, totals.

    Args:
        epm_sn: The EPM's serial number.
    """
    return _shape(await _call("/v1/api/epmDetail", {"sn": epm_sn}))


@mcp.tool(tags={"epm"}, annotations=READ)
async def get_epm_day(
    epm_sn: str,
    date: str,
    fields: str = "p_epm_total,e_total_buy,e_total_sell",
    timezone: float = 8,
) -> Any:
    """An EPM's readings for one day, for a chosen set of fields.

    Returns a dict of parallel arrays (one per requested field, plus
    data_timestamp and timeStr), not a list of sample objects. In pEpmTotal,
    negative means importing from the grid and positive means exporting.

    Args:
        epm_sn: The EPM's serial number.
        date: Date in YYYY-MM-DD format.
        fields: Comma-separated field list. Valid values: u_ac1, u_ac2, u_ac3
            (voltage U/V/W), i_ac1, i_ac2, i_ac3 (current), p_ac1, p_ac2, p_ac3
            (power), power_factor, fac_meter (grid frequency), p_load (load
            power), e_total_inverter, e_total_load, e_total_buy, e_total_sell,
            p_epm_total.
        timezone: UTC offset for the query.
    """
    return _shape(
        await _call(
            "/v1/api/epm/day",
            {"sn": epm_sn, "searchinfo": fields, "time": date, "timeZone": timezone},
        )
    )


@mcp.tool(tags={"epm"}, annotations=READ)
async def get_epm_energy(
    epm_sn: str, period: Literal["month", "year", "lifetime"], date: str = ""
) -> Any:
    """An EPM's buy/sell energy totals.

    - month: one row per day (date YYYY-MM).
    - year: one row per month (date YYYY).
    - lifetime: one row per year (no date).

    Args:
        epm_sn: The EPM's serial number.
        period: month, year or lifetime.
        date: Required for month/year, in the format above.
    """
    params: dict[str, Any] = {"sn": epm_sn}
    if period != "lifetime":
        pattern, label = _DATE_FORMATS[period]
        if not re.fullmatch(pattern, date):
            raise ToolError(f"period={period} needs date in {label} format, got {date!r}")
        params[period] = date
    path = {"month": "/v1/api/epm/month", "year": "/v1/api/epm/year", "lifetime": "/v1/api/epm/all"}
    return _shape(await _call(path[period], params))


# ---------------------------------------------------------------------------
# Weather stations
# ---------------------------------------------------------------------------


@mcp.tool(tags={"weather"}, annotations=READ)
async def list_weather_stations(
    page_no: int = 1, page_size: int = 20, station_id: str = "", nmi_code: str = ""
) -> Any:
    """List meteorological instruments under the account.

    Records carry irradiance (totalR/directR/scatteredR in W/m2), daily
    irradiation (MJ/m2), temperature, humidity, wind and rainfall. Irradiance is
    the denominator for any performance-ratio calculation.

    Args:
        page_no: 1-based page number.
        page_size: Results per page (max 100).
        station_id: Optional plant ID to filter by.
        nmi_code: Optional NMI code to filter by.
    """
    params: dict[str, Any] = {"pageNo": page_no, "pageSize": page_size}
    if station_id:
        params["stationId"] = station_id
    if nmi_code:
        params["nmiCode"] = nmi_code
    return _shape(await _call("/v1/api/weatherList", params))


@mcp.tool(tags={"weather"}, annotations=READ)
async def get_weather_detail(weather_sn: str) -> Any:
    """Full readings for one meteorological instrument.

    Temperature fields use the unit given by temperatureUnit, which may be
    Fahrenheit -- read it rather than assuming Celsius.

    Args:
        weather_sn: The instrument's serial number.
    """
    return _shape(await _call("/v1/api/weatherDetail", {"sn": weather_sn}))


# ---------------------------------------------------------------------------
# Meters
# ---------------------------------------------------------------------------


@mcp.tool(tags={"meter"}, annotations=READ)
async def list_meters(page_no: int = 1, page_size: int = 20, station_id: str = "") -> Any:
    """List electricity meters under the account.

    Records carry real-time power plus daily and lifetime forward (import) and
    reverse (export) energy.

    Args:
        page_no: 1-based page number.
        page_size: Results per page (max 100).
        station_id: Optional plant ID to filter by.
    """
    params: dict[str, Any] = {"pageNo": page_no, "pageSize": page_size}
    if station_id:
        params["stationId"] = station_id
    return _shape(await _call("/v1/api/ammeterList", params))


@mcp.tool(tags={"meter"}, annotations=READ)
async def get_meter_detail(meter_sn: str) -> Any:
    """Full details for one meter: per-phase V/I/P, power factor, energy totals.

    electricMeter indicates phase count: 0=single-phase, anything else
    three-phase. Negative power values indicate reverse flow (export).

    Args:
        meter_sn: The meter's serial number.
    """
    return _shape(await _call("/v1/api/ammeterDetail", {"sn": meter_sn}))


# ---------------------------------------------------------------------------
# Write operations
#
# These mutate the real installation, and one of them (unbind_collector with
# delete_inverters=1) removes devices along with their history. The "write"
# group is only exposed when SOLIS_ENABLE_WRITES is set, so that an agent
# exploring the read tools cannot see or reach them by accident.
# ---------------------------------------------------------------------------


@mcp.tool(
    tags={"write"},
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True),
)
async def create_station(
    station_name: str,
    capacity: str,
    money: str,
    addr: str,
    price: float,
    inverter_sn: str = "",
    collector_sn: str = "",
    user_id: str = "",
    mobile: str = "",
    latitude: str = "",
    longitude: str = "",
    dip: float | None = None,
    azimuth: float | None = None,
    nmi_code: str = "",
) -> Any:
    """Create a new plant. WRITE -- modifies the real installation.

    Args:
        station_name: Name for the new plant.
        capacity: Installed capacity in kWp.
        money: Currency code for revenue, e.g. AUD, EUR, CNY.
        addr: Street address of the plant.
        price: Revenue per kWh.
        inverter_sn: Optional inverter SN to bind on creation.
        collector_sn: Optional collector SN to bind on creation.
        user_id: Optional owner account; defaults to the API account.
        mobile: Optional owner mobile number.
        latitude: Optional latitude.
        longitude: Optional longitude.
        dip: Optional panel inclination angle.
        azimuth: Optional panel azimuth.
        nmi_code: Optional NMI code.
    """
    params: dict[str, Any] = {
        "stationName": station_name,
        "capacity": capacity,
        "money": money,
        "addr": addr,
        "price": price,
    }
    optional = {
        "inverterSn": inverter_sn,
        "collectorSn": collector_sn,
        "userId": user_id,
        "mobile": mobile,
        "latitude": latitude,
        "longitude": longitude,
        "dip": dip,
        "azimuth": azimuth,
        "nmiCode": nmi_code,
    }
    params.update({k: v for k, v in optional.items() if v not in ("", None)})
    return await _call("/v1/api/addStation", params)


@mcp.tool(
    tags={"write"},
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
)
async def update_station(
    station_id: str,
    station_name: str,
    capacity: str,
    price: float,
    addr: str,
    mobile: str = "",
    money: str = "",
    latitude: str = "",
    longitude: str = "",
    dip: float | None = None,
    azimuth: float | None = None,
    nmi_code: str = "",
    daylight_switch: int | None = None,
) -> Any:
    """Update an existing plant's information. WRITE -- modifies the real installation.

    station_name, capacity, price and addr are mandatory per the API, so read
    the current values with get_station(verbose=True) first and pass them back
    unchanged where you do not intend to alter them.

    Args:
        station_id: The plant ID to update.
        station_name: Plant name (required even if unchanged).
        capacity: Installed capacity in kWp (required even if unchanged).
        price: Revenue per kWh (required even if unchanged).
        addr: Street address (required even if unchanged).
        mobile: Optional owner mobile number.
        money: Optional currency code, e.g. AUD.
        latitude: Optional latitude.
        longitude: Optional longitude.
        dip: Optional panel inclination angle.
        azimuth: Optional panel azimuth.
        nmi_code: Optional NMI code.
        daylight_switch: Optional daylight saving: 0=off, 1=on.
    """
    params: dict[str, Any] = {
        "id": station_id,
        "stationName": station_name,
        "capacity": capacity,
        "price": price,
        "addr": addr,
    }
    optional = {
        "mobile": mobile,
        "money": money,
        "latitude": latitude,
        "longitude": longitude,
        "dip": dip,
        "azimuth": azimuth,
        "nmiCode": nmi_code,
        "daylightSwitch": daylight_switch,
    }
    params.update({k: v for k, v in optional.items() if v not in ("", None)})
    return await _call("/v1/api/stationUpdate", params)


@mcp.tool(
    tags={"write"},
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True),
)
async def create_station_with_collector(
    collector_sn: str,
    station_name: str,
    capacity: str,
    addr: str = "",
    money: str = "",
    price: float | None = None,
    user_id: str = "",
    latitude: str = "",
    longitude: str = "",
    nmi_code: str = "",
) -> Any:
    """Create a plant and bind collectors to it in one call. WRITE.

    Args:
        collector_sn: Collector SN to bind. Comma-separate for several.
        station_name: Name for the new plant.
        capacity: Installed capacity in kWp.
        addr: Optional street address.
        money: Optional currency code, e.g. AUD.
        price: Optional revenue per kWh.
        user_id: Optional owner account.
        latitude: Optional latitude.
        longitude: Optional longitude.
        nmi_code: Optional NMI code.
    """
    params: dict[str, Any] = {
        "sn": collector_sn,
        "stationName": station_name,
        "capacity": capacity,
    }
    optional = {
        "addr": addr,
        "money": money,
        "price": price,
        "userId": user_id,
        "latitude": latitude,
        "longitude": longitude,
        "nmiCode": nmi_code,
    }
    params.update({k: v for k, v in optional.items() if v not in ("", None)})
    return await _call("/v1/api/addStationBindCollector", params)


@mcp.tool(
    tags={"write"},
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True),
)
async def bind_inverters(
    inverter_sn: str, station_id: str = "", nmi_code: str = ""
) -> Any:
    """Bind one or more inverters to an existing plant. WRITE.

    Provide either station_id or nmi_code.

    Args:
        inverter_sn: Inverter SN to bind. Comma-separate for several.
        station_id: The plant ID to bind to.
        nmi_code: NMI code (alternative to station_id).
    """
    if not station_id and not nmi_code:
        raise ToolError("Provide station_id or nmi_code")
    params: dict[str, Any] = {"sn": inverter_sn}
    if station_id:
        params["id"] = station_id
    if nmi_code:
        params["nmiCode"] = nmi_code
    return await _call("/v1/api/addDevice", params)


@mcp.tool(
    tags={"write"},
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
)
async def unbind_collector(collector_sn: str, delete_inverters: int = 0) -> Any:
    """Unbind a collector from its plant. DESTRUCTIVE WRITE.

    With delete_inverters=1 this also removes every inverter behind that
    collector. There is no undo through this API. Confirm with the operator
    before calling with delete_inverters=1.

    Args:
        collector_sn: The collector's serial number.
        delete_inverters: 0 = unbind the collector only (default).
            1 = also delete all inverters attached to it.
    """
    if delete_inverters not in (0, 1):
        raise ToolError("delete_inverters must be 0 or 1")
    return await _call(
        "/v1/api/delCollector",
        {"sn": collector_sn, "deleteInvert": delete_inverters},
    )


# ---------------------------------------------------------------------------
# Toolset selection
# ---------------------------------------------------------------------------


def _enabled_toolsets() -> set[str]:
    requested = {
        s.strip().lower()
        for s in (os.environ.get("SOLIS_TOOLSETS") or "core").split(",")
        if s.strip()
    }
    if "all" in requested:
        requested = set(TOOLSETS)
    unknown = requested - set(TOOLSETS)
    if unknown:
        raise RuntimeError(
            f"Unknown SOLIS_TOOLSETS value(s): {', '.join(sorted(unknown))}. "
            f"Choose from: {', '.join(TOOLSETS)}, all"
        )
    # Writes are gated on their own flag, never on SOLIS_TOOLSETS alone.
    requested.discard("write")
    if os.environ.get("SOLIS_ENABLE_WRITES", "").strip().lower() in {"1", "true", "yes"}:
        requested.add("write")
    return requested


mcp.enable(tags=_enabled_toolsets(), only=True)

# Keep serving so the client can connect, but say up front why every tool
# call is going to fail.
for _problem in credential_problems():
    log.error("SolisCloud credentials: %s. Every tool call will fail until this is fixed.", _problem)


# def main() -> None:
#     mcp.run(transport="stdio")


# if __name__ == "__main__":
#     main()
app = mcp.http_app(
    path="/mcp"
)
