# API behaviour worth knowing

These were measured against the live API, not just read from the spec. Several
contradict the official document.

## Data refreshes every 5 minutes

All endpoints update on a 5-minute server-side cycle (spec §1.2). Calling again
sooner returns identical values. Historical buckets — past days, months, years —
are final and never change.

## Rate limits

2 requests/sec on most endpoints; 10/sec on `stationMonthEnergyList` and
`stationYearEnergyList`. On multi-plant accounts, prefer the batch tools in the `fleet` group
(`list_inverter_details`, `list_station_details`, `list_station_energy`) over
looping the singular ones.

## Numeric values are scaled — read the `*Pec` factor

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

## Power samples cannot be summed into energy

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

## Timezone is per-device and mostly ignored

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

## The spec's "Required" column is unreliable

Parameters marked Required=Y that are in fact optional — all verified returning
full data when omitted:

- `money` on `inverterDay`, `inverterMonth`, `inverterYear`, `inverterAll`,
  `stationDay`, `stationMonth`, `stationYear`, `stationAll`
- `timeZone` on `stationMonth`, `stationYear`

## Errors can arrive with HTTP 200

SolisCloud wraps responses as `{success, code, msg, data}`. `soliscloud_client.py`
raises on `success: false` and returns only `data`, which discards `code`/`msg` —
so a diagnostic like `B0049` ("collector does not exist or no permission") is
lost. See [Known gaps](development.md#known-gaps).
