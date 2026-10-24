"""Core tools end to end over an in-process MCP client, against fixture data."""

import asyncio
import copy
import os
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest
from fastmcp import Client

import server
from soliscloud_client import SolisCloudAPIError

STATION_ID = "1000000000000000001"
INVERTER_ID = "1000000000000000002"


def test_default_toolset_is_four_read_only_tools():
    async def run():
        async with Client(server.mcp) as client:
            return await client.list_tools()

    tools = asyncio.run(run())
    assert {t.name for t in tools} == {"get_station", "get_inverter", "get_energy", "list_alarms"}
    assert all(t.annotations.read_only_hint for t in tools)


# --- get_station / get_inverter ----------------------------------------------


def test_get_station_summary(api, call):
    err, data = call("get_station")
    assert not err
    # The id is resolved from the account's only plant, then cached.
    assert api.paths() == ["/v1/api/userStationList", "/v1/api/stationDetail"]
    assert data["id"] == STATION_ID
    assert (data["power"], data["powerStr"]) == (2.26, "kW")
    assert (data["dayEnergy"], data["dayEnergyStr"]) == (27.1, "kWh")
    assert data["yearEnergyStr"] == "MWh"
    assert data["updated"] == "2026-10-22T15:55:14+11:00"
    assert not any(k.endswith("Pec") for k in data)
    assert "userEmail" not in data and "nmiCode" not in data
    assert len(data) < 60

    call("get_station")
    assert api.paths().count("/v1/api/userStationList") == 1


def test_get_station_verbose_returns_everything(api, call):
    err, data = call("get_station", {"station_id": STATION_ID, "verbose": True})
    assert not err
    assert len(data) > 200
    assert not any(k.endswith("Pec") for k in data)


def test_get_inverter_keeps_only_live_strings(api, call):
    err, data = call("get_inverter")
    assert not err
    assert (data["pac"], data["pacStr"]) == (2.26, "kW")
    assert {"uPv1", "iPv1", "pow1", "uPv2", "uAc1"} <= data.keys()
    assert not {"uPv3", "pow3", "uAc2"} & data.keys()  # zero on this inverter
    assert data["updated"].endswith("+11:00")


def test_get_inverter_by_sn(api, call):
    err, _ = call("get_inverter", {"inverter_sn": "TESTINV00000001"})
    assert not err
    assert api.calls == [("/v1/api/inverterDetail", {"sn": "TESTINV00000001"})]


def test_unknown_inverter_is_an_error(api, call):
    api.responses["/v1/api/inverterDetail"] = None  # what the API returns
    err, text = call("get_inverter", {"inverter_id": "123"})
    assert err
    assert "No inverter found" in text


def test_several_plants_require_an_id(api, call):
    listing = copy.deepcopy(api.responses["/v1/api/userStationList"])
    second = dict(listing["page"]["records"][0], id="42", stationName="Shed")
    listing["page"]["records"].append(second)
    api.responses["/v1/api/userStationList"] = listing

    err, text = call("get_station")
    assert err
    assert STATION_ID in text and "42 (Shed)" in text


# --- get_energy ---------------------------------------------------------------


def test_inverter_day_is_scaled_and_trimmed(api, call):
    err, rows = call("get_energy", {"period": "day", "date": "2026-10-20", "target": "inverter"})
    assert not err
    assert len(rows) == 5
    assert all(set(r) <= {"timeStr", "pac", "pacStr", "eToday", "eTodayStr"} for r in rows)
    peak = max(rows, key=lambda r: r["pac"])
    assert (peak["pac"], peak["pacStr"]) == (4.77, "kW")
    assert rows[-1]["eToday"] == 25.2


@pytest.mark.parametrize(
    ("day", "offset"),
    [("2026-10-20", 11), ("2026-07-15", 10), ("2026-04-05", 10), ("2026-10-04", 11)],
)
def test_inverter_day_sends_daylight_saving_offset(api, call, day, offset):
    call("get_energy", {"period": "day", "date": day, "target": "inverter", "target_id": INVERTER_ID})
    assert api.params("/v1/api/inverterDay") == [
        {"id": INVERTER_ID, "time": day, "timeZone": offset}
    ]


def test_inverter_zone_is_looked_up_once(api, call):
    for day in ("2026-10-20", "2026-07-15"):
        call("get_energy", {"period": "day", "date": day, "target": "inverter", "target_id": INVERTER_ID})
    assert api.paths().count("/v1/api/inverterDetail") == 1


def test_default_timezone_override_skips_lookup(api, call, monkeypatch):
    monkeypatch.setenv("SOLIS_DEFAULT_TIMEZONE", "9.5")
    call("get_energy", {"period": "day", "date": "2026-10-20", "target": "inverter", "target_id": INVERTER_ID})
    assert api.params("/v1/api/inverterDay")[0]["timeZone"] == 9.5
    assert "/v1/api/inverterDetail" not in api.paths()


def test_station_day_never_sends_timezone(api, call):
    err, rows = call("get_energy", {"period": "day", "date": "2026-10-20", "target_id": STATION_ID})
    assert not err
    assert api.params("/v1/api/stationDay") == [{"id": STATION_ID, "time": "2026-10-20"}]
    assert max(r["power"] for r in rows) == 4.77


def test_month_rows(api, call):
    err, rows = call("get_energy", {"period": "month", "date": "2026-10", "target": "inverter"})
    assert not err
    assert api.params("/v1/api/inverterMonth")[0] == {"id": INVERTER_ID, "month": "2026-10"}
    assert rows[0] == {"dateStr": "2026-10-01", "energy": 2.6, "energyStr": "kWh", "fullHour": 0.52}


def test_lifetime_rows_are_scaled(api, call):
    err, rows = call("get_energy", {"period": "lifetime", "target_id": STATION_ID})
    assert not err
    assert api.params("/v1/api/stationAll") == [{"id": STATION_ID}]
    row_2024 = next(r for r in rows if r["dateStr"] == "2024")
    assert (row_2024["energy"], row_2024["energyStr"]) == (6.594, "MWh")


def test_custom_fields_keep_the_time_label(api, call):
    err, rows = call(
        "get_energy",
        {"period": "day", "date": "2026-10-20", "target": "inverter", "fields": "uPv1"},
    )
    assert not err
    assert all(set(r) <= {"timeStr", "uPv1", "uPv1Str"} for r in rows)
    assert all("timeStr" in r for r in rows)


def test_all_fields(api, call):
    err, rows = call(
        "get_energy",
        {"period": "day", "date": "2026-10-20", "target": "inverter", "fields": "all"},
    )
    assert not err
    assert len(rows[0]) > 20
    assert not any(k.endswith("Pec") for k in rows[0])


@pytest.mark.parametrize(
    ("period", "date"),
    [("day", "20/10/2026"), ("month", "Oct 2026"), ("year", "26"), ("day", "")],
)
def test_bad_date_is_rejected_before_any_request(api, call, period, date):
    err, text = call("get_energy", {"period": period, "date": date})
    assert err
    assert "format" in text
    assert api.calls == []


# --- errors and alarms --------------------------------------------------------


def test_api_error_becomes_tool_error(api, call):
    api.responses["/v1/api/stationDetail"] = SolisCloudAPIError("no permission", code="B0049")
    err, text = call("get_station", {"station_id": STATION_ID})
    assert err
    assert "B0049" in text and "no permission" in text


def test_network_error_becomes_tool_error(api, call):
    api.responses["/v1/api/stationDetail"] = urllib.error.URLError("connection refused")
    err, text = call("get_station", {"station_id": STATION_ID})
    assert err
    assert "Could not reach SolisCloud" in text


def test_missing_credentials_is_tool_error(fresh_server, call, monkeypatch):
    monkeypatch.setattr(server, "_client", None)
    monkeypatch.delenv("SOLIS_KEY_ID", raising=False)
    err, text = call("get_station", {"station_id": STATION_ID})
    assert err
    assert "SOLIS_KEY_ID" in text


@pytest.mark.parametrize(
    "env, expected",
    [
        ({"SOLIS_KEY_ID": "", "SOLIS_KEY_SECRET": "s"}, "SOLIS_KEY_ID is not set or empty"),
        ({"SOLIS_KEY_ID": "k", "SOLIS_KEY_SECRET": "  "}, "SOLIS_KEY_SECRET is not set or empty"),
        (
            {"SOLIS_KEY_ID": "op://Private/Solis/key id", "SOLIS_KEY_SECRET": "s"},
            "unresolved 1Password op:// reference in SOLIS_KEY_ID",
        ),
        (
            {"SOLIS_KEY_ID": "k", "SOLIS_KEY_SECRET": "s", "SOLIS_API_BASE_URL": "soliscloud.com"},
            "SOLIS_API_BASE_URL must start with https://",
        ),
    ],
)
def test_bad_credentials_name_the_variable(fresh_server, call, monkeypatch, env, expected):
    monkeypatch.setattr(server, "_client", None)
    monkeypatch.delenv("SOLIS_API_BASE_URL", raising=False)
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    err, text = call("get_station", {"station_id": STATION_ID})
    assert err
    assert expected in text


def test_empty_base_url_uses_default(fresh_server, monkeypatch):
    monkeypatch.setattr(server, "_client", None)
    monkeypatch.setenv("SOLIS_KEY_ID", "k")
    monkeypatch.setenv("SOLIS_KEY_SECRET", "s")
    monkeypatch.setenv("SOLIS_API_BASE_URL", "")
    assert server.credential_problems() == []
    assert server.get_client()._base_url == "https://www.soliscloud.com:13333"


def test_alarms_are_trimmed_with_utc_times(api, call):
    err, data = call("list_alarms", {"station_id": STATION_ID})
    assert not err
    assert data["total"] == 4
    first = data["records"][0]
    assert set(first) <= set(server.ALARM_FIELDS)
    assert first["alarmBeginTime"] == "2025-05-12T00:02:10+00:00"
    assert first["alarmMsg"] == "GRID INTF"


def test_server_refuses_to_start_without_credentials():
    env = {k: v for k, v in os.environ.items() if not k.startswith("SOLIS_")}
    result = subprocess.run(
        [sys.executable, "-c", "import server"],
        cwd=Path(server.__file__).parent,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "SOLIS_KEY_ID is not set or empty" in result.stderr
    assert "Refusing to start" in result.stderr
