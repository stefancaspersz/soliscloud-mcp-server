"""Shared fixtures: an offline stand-in for the SolisCloud API.

The JSON under tests/fixtures/ was captured from a live account on 2026-10-22
and scrubbed: ids, serials and the NMI replaced with fakes, email, address and
locality removed, and day series trimmed to five rows.
"""

import asyncio
import json
import os
from pathlib import Path

import pytest

# Tool visibility is decided when server.py is imported, so pin the toolset
# environment first: the suite always sees the default (core) tools.
for _var in ("SOLIS_TOOLSETS", "SOLIS_ENABLE_WRITES", "SOLIS_DEFAULT_TIMEZONE"):
    os.environ.pop(_var, None)

import server  # noqa: E402
from fastmcp import Client  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"

ROUTES = {
    "/v1/api/userStationList": "station_list",
    "/v1/api/inverterList": "inverter_list",
    "/v1/api/stationDetail": "station_detail",
    "/v1/api/inverterDetail": "inverter_detail",
    "/v1/api/alarmList": "alarm_list",
    "/v1/api/stationDay": "station_day",
    "/v1/api/inverterDay": "inverter_day",
    "/v1/api/inverterMonth": "inverter_month",
    "/v1/api/stationAll": "station_all",
}


def load(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text())


class FakeAPI:
    """Replays fixture responses by path and records every request.

    Set responses[path] to an exception instance to make that path fail.
    """

    def __init__(self):
        self.responses = {path: load(name) for path, name in ROUTES.items()}
        self.calls: list[tuple[str, dict]] = []

    async def call(self, path, params=None):
        self.calls.append((path, params or {}))
        response = self.responses[path]
        if isinstance(response, Exception):
            raise response
        return response

    def paths(self):
        return [path for path, _ in self.calls]

    def params(self, path):
        return [params for p, params in self.calls if p == path]


@pytest.fixture
def fresh_server(monkeypatch):
    """Clear the server's per-process caches."""
    monkeypatch.setattr(server, "_tz_cache", {})
    monkeypatch.setattr(server, "_only_ids", {})
    return server


@pytest.fixture
def api(fresh_server, monkeypatch):
    fake = FakeAPI()
    monkeypatch.setattr(server, "_client", fake)
    return fake


@pytest.fixture
def call():
    """Call a tool in-process. Returns (is_error, parsed JSON or error text)."""

    def _call(name, args=None):
        async def run():
            async with Client(server.mcp) as client:
                return await client.call_tool(name, args or {}, raise_on_error=False)

        result = asyncio.run(run())
        text = result.content[0].text
        return result.is_error, (text if result.is_error else json.loads(text))

    return _call
