"""Unit tests for response shaping, timezone offsets and toolset selection."""

import pytest

import server


# --- _scale -----------------------------------------------------------------


def test_scale_applies_pec_and_drops_it():
    row = server._scale({"pac": 4640.0, "pacPec": "0.001", "pacStr": "kW"})
    assert row == {"pac": 4.64, "pacStr": "kW"}


def test_scale_handles_mwh_lifetime_energy():
    row = server._scale({"energy": 6594.0, "energyPec": "0.001", "energyStr": "MWh"})
    assert row["energy"] == 6.594


def test_scale_factor_of_one_leaves_value_untouched():
    row = server._scale({"pow1": 1360, "pow1Pec": "1"})
    assert row == {"pow1": 1360}
    assert isinstance(row["pow1"], int)


def test_scale_keeps_factor_beside_unscalable_value():
    row = server._scale({"x": "n/a", "xPec": "0.001"})
    assert row == {"x": "n/a", "xPec": "0.001"}


def test_scale_drops_orphan_factor_and_ignores_bad_factor():
    assert server._scale({"yPec": "0.001"}) == {}
    assert server._scale({"z": 5, "zPec": "?"}) == {"z": 5, "zPec": "?"}


def test_scale_does_not_treat_booleans_as_numbers():
    assert server._scale({"b": True, "bPec": "0.001"}) == {"b": True, "bPec": "0.001"}


# --- _compact / _shape ------------------------------------------------------


def test_compact_drops_empty_values_but_keeps_zero_and_false():
    data = {"a": None, "b": "", "c": [], "d": {}, "e": 0, "f": False, "g": {"h": None}}
    assert server._compact(data) == {"e": 0, "f": False}


def test_shape_unwraps_page_and_projects_fields_with_units():
    data = {
        "stationStatusVo": {"all": 1},
        "page": {
            "total": 1,
            "records": [{"id": "1", "pac": 2260.0, "pacPec": "0.001", "pacStr": "kW", "x": 1}],
        },
    }
    assert server._shape(data, ["id", "pac"]) == {
        "total": 1,
        "records": [{"id": "1", "pac": 2.26, "pacStr": "kW"}],
    }


def test_shape_keeps_unit_companions():
    row = {"dayInCome": 1.46, "dayInComeUnit": "AUD", "other": 1}
    assert server._shape(row, ["dayInCome"]) == {"dayInCome": 1.46, "dayInComeUnit": "AUD"}


def test_parse_fields():
    assert server._parse_fields("", ["a"]) == ["a"]
    assert server._parse_fields(" uPv1, fac ,", ["a"]) == ["uPv1", "fac"]
    assert server._parse_fields("ALL", ["a"]) is None


# --- time -------------------------------------------------------------------


def test_local_time_uses_iana_zone_for_daylight_saving():
    # 2026-10-22 04:55:14 UTC is 15:55 in Canberra (AEDT, +11), not 14:55.
    ms = 1792644914065
    assert server._local_time(ms, "Australia/Canberra") == "2026-10-22T15:55:14+11:00"
    assert server._local_time(ms, 10.0) == "2026-10-22T14:55:14+10:00"
    assert server._local_time(ms) == "2026-10-22T04:55:14+00:00"
    assert server._local_time(None, "Australia/Canberra") is None
    assert server._local_time(ms, "Not/AZone") is None


@pytest.mark.parametrize(
    ("day", "offset"),
    [
        ("2026-07-15", 10),  # AEST
        ("2026-10-20", 11),  # AEDT
        ("2026-04-05", 10),  # DST ended 03:00 that morning
        ("2026-10-04", 11),  # DST started 02:00 that morning
    ],
)
def test_offset_on_follows_daylight_saving(day, offset):
    assert server._offset_on("Australia/Canberra", day) == offset


def test_offset_on_fixed_offsets():
    assert server._offset_on(10.0, "2026-10-20") == 10
    assert server._offset_on(9.5, None) == 9.5
    assert server._offset_on("Australia/Adelaide", "2026-07-15") == 9.5


# --- toolsets ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("toolsets", "writes", "expected"),
    [
        (None, None, {"core"}),
        ("", None, {"core"}),
        ("core, Collector", None, {"core", "collector"}),
        ("core,write", None, {"core"}),  # write needs its own flag
        ("core", "1", {"core", "write"}),
        ("all", None, set(server.TOOLSETS) - {"write"}),
        ("all", "yes", set(server.TOOLSETS)),
    ],
)
def test_enabled_toolsets(monkeypatch, toolsets, writes, expected):
    for var, value in (("SOLIS_TOOLSETS", toolsets), ("SOLIS_ENABLE_WRITES", writes)):
        if value is None:
            monkeypatch.delenv(var, raising=False)
        else:
            monkeypatch.setenv(var, value)
    assert server._enabled_toolsets() == expected


def test_unknown_toolset_fails_loudly(monkeypatch):
    monkeypatch.setenv("SOLIS_TOOLSETS", "core,bogus")
    with pytest.raises(RuntimeError, match="bogus"):
        server._enabled_toolsets()
