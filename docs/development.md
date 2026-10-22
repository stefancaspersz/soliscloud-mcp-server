# Development

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
[The spec's "Required" column is unreliable](api-behaviour.md#the-specs-required-column-is-unreliable).
If you download a newer version, the measured behaviours in
[API behaviour](api-behaviour.md) are worth re-checking against it.

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
