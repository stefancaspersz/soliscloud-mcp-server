# soliscloud-mcp-server

An MCP (Model Context Protocol) server that exposes the [SolisCloud](https://www.soliscloud.com)
monitoring API as tools an LLM client (Claude Desktop, Claude Code, etc.) can
call.

Covers all 37 endpoints in the SolisCloud Platform API Document
[**V2.0.3**](#api-specification) (revised 24 Dec 2025): plants, inverters,
collectors, EPM devices, weather stations, meters, alarms, and the
plant-management write operations.

## 1. Get SolisCloud API credentials

You need an active SolisCloud account plus API access, which SolisCloud grants
manually per request:

1. Log in at https://www.soliscloud.com.
2. Go to **Service → API Management** (or https://www.soliscloud.com/#/apiManage).
3. Activate API access and agree to the terms. You may need to raise a support
   ticket first — see https://solis-service.solisinverters.com if the option
   isn't visible.
4. Once approved, note your **KeyId**, **KeySecret**, and the **API base URL**
   you're given (usually `https://www.soliscloud.com:13333`).

Keep the KeySecret private — treat it like a password.

## 2. Install

```bash
git clone https://github.com/stefancaspersz/soliscloud-mcp-server.git
cd soliscloud-mcp-server
python3 -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 3. Configure credentials

Copy `.env.example` to `.env` and fill in your values, or export the variables
in your shell / MCP client config:

| Variable | Required | Purpose |
|---|---|---|
| `SOLIS_KEY_ID` | yes | API key ID issued by SolisCloud |
| `SOLIS_KEY_SECRET` | yes | API key secret issued by SolisCloud |
| `SOLIS_API_BASE_URL` | no | Defaults to `https://www.soliscloud.com:13333` |
| `SOLIS_DEFAULT_TIMEZONE` | no | Fixed UTC offset (e.g. `10`, `9.5`). Overrides the automatic per-device lookup, including its daylight-saving handling. Normally unnecessary. |
| `SOLIS_TOOLSETS` | no | Comma-separated tool groups to expose (see [Tools](#tools)). Defaults to `core`; `all` exposes every read group. |
| `SOLIS_ENABLE_WRITES` | no | Set to `1` to also expose the five tools that modify the installation. The only way to enable the `write` group. |

## 4. Run it

`server.py` currently exposes an **HTTP** app (`app = mcp.http_app(path="/mcp")`):

```bash
.venv/bin/uvicorn server:app --host 127.0.0.1 --port 8765
```

The MCP endpoint is then `http://127.0.0.1:8765/mcp`. A bare `GET` returns
`400 Bad Request` — that's expected, MCP needs the protocol handshake.

To run over **stdio** instead (what most desktop clients expect), uncomment the
`main()` block at the bottom of `server.py`:

```python
def main() -> None:
    mcp.run(transport="stdio")

if __name__ == "__main__":
    main()
```

To sanity-check credentials without any MCP client:

```bash
python -c "
import asyncio, os
from dotenv import load_dotenv
from soliscloud_client import SolisCloudClient

load_dotenv()  # reads .env in the current directory

async def main():
    client = SolisCloudClient(os.environ['SOLIS_KEY_ID'], os.environ['SOLIS_KEY_SECRET'])
    print(await client.call('/v1/api/userStationList', {'pageNo': 1, 'pageSize': 10}))
    await client.aclose()

asyncio.run(main())
"
```

(`load_dotenv()` only works if you run this from inside `soliscloud-mcp-server/` where
your `.env` file lives — otherwise export the variables first, or pass the path
explicitly: `load_dotenv('/path/to/.env')`.)

## 5. Add it to an MCP client

### Claude Desktop / Claude Code (stdio)

Requires the `main()` block above to be uncommented. Add to your MCP config
(e.g. `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "soliscloud": {
      "command": "/absolute/path/to/soliscloud-mcp-server/.venv/bin/python",
      "args": ["/absolute/path/to/soliscloud-mcp-server/server.py"],
      "env": {
        "SOLIS_KEY_ID": "your_key_id",
        "SOLIS_KEY_SECRET": "your_key_secret",
        "SOLIS_API_BASE_URL": "https://www.soliscloud.com:13333"
      }
    }
  }
}
```

Restart the client and the `soliscloud` tools should appear.

---

## API behaviour worth knowing

These were measured against the live API, not just read from the spec. Several
contradict the official document.

### Data refreshes every 5 minutes

All endpoints update on a 5-minute server-side cycle (spec §1.2). Calling again
sooner returns identical values. Historical buckets — past days, months, years —
are final and never change.

### Rate limits

2 requests/sec on most endpoints; 10/sec on `stationMonthEnergyList` and
`stationYearEnergyList`. On multi-plant accounts, prefer the batch tools in the `fleet` group
(`list_inverter_details`, `list_station_details`, `list_station_energy`) over
looping the singular ones.

### Numeric values are scaled — read the `*Pec` factor

**This is the easiest way to be wrong by 1000×.** A field's true value is the
raw number multiplied by its companion `*Pec` factor, in the unit given by its
companion `*Str` field:

```json
{ "pac": 3340.0, "pacPec": "0.001", "pacStr": "kW" }
```

That is **3.34 kW**, not 3340 kW. `*Pec` is frequently `0.001`. Units are not
fixed across responses either — the same field can come back in kWh or MWh, so
read `*Str` rather than assuming.

Verified on every endpoint (e.g. lifetime `energy` 6594 × 0.001 = 6.594 MWh),
so **the server applies it for you**: tools return scaled values with the
`*Pec` fields removed, keeping `*Str`/`*Unit` for the unit.

### Power samples cannot be summed into energy

`get_energy(period="day")` returns *instantaneous power* (`pac` for an
inverter, `power` for a station),
not energy. Measured on one real day (7.9 kWh actual):

| Method | Result | Error |
|---|---|---|
| `sum(pac)` | 112,120 | 1,419,140% |
| `sum(pac) × 5/60` | 9,343 | 118,170% |
| `sum(pac × pacPec) × 5/60` | 9.343 | 18.3% |
| final `eToday` | **7.9** | — |
| month view `energy` | **7.9 kWh** | exact |

Integration fails even when done correctly because sampling is **not** uniformly
5 minutes: 153 samples over 644 minutes, mostly 5.0–5.1 min gaps but with bursts
under 1 minute.

Use the counters instead. `eToday` is monotonic within the day, and the final
sample's value matches the inverter month view exactly. `stationDay` has **no**
cumulative energy field — get the daily total from `get_energy(period="month")`
or `get_station`'s `dayEnergy`.

### Timezone is per-device and mostly ignored

Day/month/year buckets follow the *device's* timezone. Only one endpoint
actually honours the `timeZone` request parameter:

| Endpoint | `timeZone` param |
|---|---|
| `inverterDay` | **honoured** — 8 vs 10 shifts every timestamp by 2h |
| `inverterMonth` | ignored — byte-identical responses |
| `stationDay` | ignored — already returns plant-local time |
| `stationMonth` | ignored — byte-identical responses |

So `get_energy` (inverter, day) resolves the offset automatically from the device record
(cached per device; one lookup). The rest never send it — a lookup there would
cost a request against the rate limit for no effect.

**Daylight saving.** The device's `timeZone` is its *standard* offset (10 for
Canberra) and ignores DST. Sending it for a date in AEDT put every inverterDay
timestamp an hour early (first sample 05:22 vs the station's 06:20). The server
therefore reads the device's `timeZoneStandardId` (e.g. `Australia/Canberra`)
and sends that zone's offset for the requested date: 11 in summer, 10 in
winter. Verified against `stationDay` on dates either side of both changeovers
(median gap ≤ 3 min, the station's 5-minute grid). A fixed
`SOLIS_DEFAULT_TIMEZONE` disables this and is wrong for half the year in a DST
zone.

The `timeZone` field *inside* response rows is not a reliable echo: `inverterDay`
rows report 8 regardless of what was sent, `inverterMonth` rows report the
device's real value, station rows report 8.

### The spec's "Required" column is unreliable

Parameters marked Required=Y that are in fact optional — all verified returning
full data when omitted:

- `money` on `inverterDay`, `inverterMonth`, `inverterYear`, `inverterAll`,
  `stationDay`, `stationMonth`, `stationYear`, `stationAll`
- `timeZone` on `stationMonth`, `stationYear`

### Errors can arrive with HTTP 200

SolisCloud wraps responses as `{success, code, msg, data}`. `soliscloud_client.py`
raises on `success: false` and returns only `data`, which discards `code`/`msg` —
so a diagnostic like `B0049` ("collector does not exist or no permission") is
lost. See Known gaps.

---

## Tools

Tools are grouped, and only the groups named in `SOLIS_TOOLSETS` are exposed
(default `core`). Every tool description is sent with each request, so the
default keeps the list to four tools (~1k tokens).

Responses are trimmed: values are scaled (see above), empty fields dropped,
and the core tools return a curated summary by default. Pass `verbose=True`
(`get_station`, `get_inverter`) or `fields="all"` (`get_energy`) for the full
record. Errors come back as MCP tool errors (`isError: true`), and read tools
carry `readOnlyHint` annotations so clients can auto-approve them.

### `core` (default)

| Tool | Description |
|---|---|
| `get_station` | Live power, energy and income totals for a plant. Id optional with one plant. |
| `get_inverter` | Live AC/DC/grid readings, temperature, fault state. Id optional with one inverter. |
| `get_energy` | History for a plant or inverter: `period` = `day` (5-min power series), `month`, `year` or `lifetime`. |
| `list_alarms` | Device alarms/faults, filterable by plant and date range |

### Optional groups

| Group | Tools |
|---|---|
| `fleet` | `list_stations`, `list_inverters`, `list_station_details`, `list_inverter_details`, `list_station_energy` |
| `collector` | `list_collectors` (check `state` first when data goes stale), `get_collector_detail`, `get_collector_day` |
| `warranty` | `get_inverter_warranty` |
| `epm` | `list_epms`, `get_epm_detail`, `get_epm_day`, `get_epm_energy` |
| `weather` | `list_weather_stations`, `get_weather_detail` |
| `meter` | `list_meters`, `get_meter_detail` |

### `write` — disabled by default

Exposed only when `SOLIS_ENABLE_WRITES=1` (adding `write` to `SOLIS_TOOLSETS`
alone does nothing). These modify the real installation.

| Tool | Description |
|---|---|
| `create_station` | Create a new plant |
| `update_station` | Update plant information |
| `create_station_with_collector` | Create a plant and bind collectors |
| `bind_inverters` | Bind inverters to an existing plant |
| `unbind_collector` | **Destructive.** Unbind a collector; with `delete_inverters=1` also deletes every inverter behind it, with no undo. |

`update_station` requires `stationName`, `capacity`, `price` and `addr` on
every call. Read current values with `get_station(verbose=True)` and pass them
back unchanged, or you will blank them.

## API specification

This server is built against:

| | |
|---|---|
| Document | SolisCloud Platform API Document |
| Version | **V2.0.3** |
| Revised | 24 Dec 2025 (added alarm status, radiation value, SOC, DST switch, standard time zone name) |
| Source | https://usservice.solisinverters.com/support/solutions/articles/73000591225-soliscloud-api-guide |

The PDF is not kept in this repo — download it from the link above when you need
field-level detail. Endpoint paths, parameter names and the `*Pec`/`*Str` field
conventions all come from that document.

Note that several of its "Required" markings are wrong in practice — see
[The spec's "Required" column is unreliable](#the-specs-required-column-is-unreliable)
above. If you download a newer version, the measured behaviours in this README
are worth re-checking against it.

A well-tested independent implementation of the same signing scheme and
endpoints: https://github.com/hultenvp/soliscloud_api

## How authentication works

SolisCloud signs requests with HMAC-SHA1, similar to Aliyun OSS:

```
StringToSign = "POST" + "\n" + Content-MD5 + "\n" + "application/json" + "\n" + Date + "\n" + Path
Signature    = Base64(HMAC-SHA1(StringToSign, KeySecret))
Authorization: API {KeyId}:{Signature}
```

Implemented in `soliscloud_client.py`. Your machine's clock must be within ~15
minutes of real time — SolisCloud rejects requests with a stale `Date` header.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The default run is offline (well under a second). It replays real API
responses from `tests/fixtures/`, captured from a live account and scrubbed
(fake ids and serials, no email, address or NMI), through an in-process MCP
client.

| File | Covers |
|---|---|
| `test_shaping.py` | `*Pec` scaling, field trimming, timestamps, DST offsets either side of both changeovers, `SOLIS_TOOLSETS` / `SOLIS_ENABLE_WRITES` selection |
| `test_tools.py` | The core tools end to end: default tool list and annotations, id auto-resolution, the inverter-day `timeZone` sent per date, date validation, API/network errors as `isError` results, alarms |
| `test_live.py` | Opt-in smoke test against the real API (read-only, ~30 s) |

The live test checks what the fixtures take on trust: scaled values are sane
magnitudes, the inverter day series lines up with the station's local-time
series (catches a wrong DST offset), and the final `eToday` matches the month
view. It is skipped unless enabled:

```bash
SOLIS_LIVE=1 op run --env-file=.env -- pytest tests/test_live.py
```

## Known gaps

- **The EPM, weather and meter tools are untested against live data.** The
  account used for development has no such devices, so `get_epm_detail`,
  `get_epm_day`, `get_epm_energy`, `get_meter_detail` and `get_weather_detail`
  were built from the spec alone.
  `get_epm_day` is the most likely to need adjustment — it takes a `searchinfo`
  field list and returns parallel arrays rather than sample objects.
- **Tools are annotated `-> Any`**, so FastMCP emits no structured-output schema
  and clients receive raw JSON text rather than typed results.
- **`code`/`msg` are discarded** on success — `soliscloud_client.call()` returns
  only `data`, losing diagnostics on `success: true` responses carrying an error
  code.
- Field names vary by endpoint. When in doubt, consult the spec — see
  [API specification](#api-specification) for the version this was built against
  and where to download it.
