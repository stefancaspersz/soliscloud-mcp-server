"""Opt-in smoke test against the real SolisCloud API. Read-only.

Skipped unless SOLIS_LIVE=1 and credentials are in the environment:

    SOLIS_LIVE=1 op run --env-file=.env -- pytest tests/test_live.py

Checks the behaviours the offline suite takes on trust from the fixtures:
that *Pec scaling still yields sane magnitudes, that the inverter day series
lines up with the station's local-time series (the daylight-saving offset),
and that the day's final eToday matches the month view.
"""

import os
import statistics
import time
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("SOLIS_LIVE") != "1" or not os.environ.get("SOLIS_KEY_ID"),
    reason="live API test; set SOLIS_LIVE=1 and SolisCloud credentials",
)


@pytest.fixture
def live(fresh_server, call):
    """call(), but paced under the 2 requests/sec limit and failing on errors."""

    def _live(name, args=None):
        time.sleep(1.2)  # a tool can make two requests (id or timezone lookup)
        err, data = call(name, args)
        assert not err, f"{name} {args}: {data}"
        return data

    return _live


def _minutes(time_str):
    hours, minutes = time_str[-8:-3].split(":")
    return int(hours) * 60 + int(minutes)


def test_live_smoke(live):
    station = live("get_station")
    assert station["capacity"] > 0
    # Unscaled values would be ~1000x too large.
    assert 0 <= station["power"] <= station["capacity"] * 1.2
    assert station["dayEnergyStr"] in {"kWh", "MWh"}

    inverter = live("get_inverter")
    assert 0 <= inverter["pac"] <= inverter["power"] * 1.2

    # A complete past day; two days back is "yesterday" in every timezone.
    day = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
    inv_day = live("get_energy", {"period": "day", "date": day, "target": "inverter"})
    st_day = live("get_energy", {"period": "day", "date": day})
    assert inv_day and st_day

    # Station rows are in plant-local time. Matching samples by identical power
    # readings, the inverter series should agree to within the station's
    # 5-minute grid; a wrong DST offset shows up as ~60 min.
    station_time = {}
    for row in st_day:
        station_time.setdefault(row.get("power"), row["timeStr"])
    gaps = [
        _minutes(row["timeStr"]) - _minutes(station_time[row["pac"]])
        for row in inv_day
        if row.get("pac", 0) > 0.5 and row["pac"] in station_time
    ]
    assert len(gaps) >= 10, "too few matching samples to compare series"
    assert abs(statistics.median(gaps)) <= 10

    month = live("get_energy", {"period": "month", "date": day[:7], "target": "inverter"})
    month_row = next(row for row in month if row["dateStr"] == day)
    assert month_row["energy"] == pytest.approx(inv_day[-1]["eToday"], abs=0.2)
