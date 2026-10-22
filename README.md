# soliscloud-mcp-server

An MCP (Model Context Protocol) server that exposes the [SolisCloud](https://www.soliscloud.com)
monitoring API as tools an LLM client (Claude Desktop, Claude Code, etc.) can
call.

Covers all 37 endpoints in the SolisCloud Platform API Document
[**V2.0.3**](docs/development.md#api-specification) (revised 24 Dec 2025): plants, inverters,
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

### Docker

```bash
docker build -t soliscloud-mcp-server:latest .
docker run -d --name soliscloud-mcp-server --env-file .env -p 127.0.0.1:8765:8765 soliscloud-mcp-server:latest
```

The server has no authentication of its own, so keep the port bound to
`127.0.0.1`. See [docs/docker.md](docs/docker.md) for 1Password `op run`,
rebuilding, and Apple `container`.

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

### Claude Code (HTTP, e.g. the Docker container)

Point the client at the running server instead of launching it. In a project's
`.mcp.json`:

```json
{
  "mcpServers": {
    "soliscloud": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

or from the command line:

```bash
claude mcp add --transport http soliscloud http://127.0.0.1:8765/mcp
```

Credentials live in the server's environment, not in the client config.

---

## Tools

Tools are grouped, and only the groups named in `SOLIS_TOOLSETS` are exposed
(default `core`). Every tool description is sent with each request, so the
default keeps the list to four tools (~1k tokens).

Responses are trimmed: values are scaled (see
[API behaviour](docs/api-behaviour.md#numeric-values-are-scaled--read-the-pec-factor)),
empty fields dropped, and the core tools return a curated summary by default.
Pass `verbose=True` (`get_station`, `get_inverter`) or `fields="all"`
(`get_energy`) for the full record. Errors come back as MCP tool errors
(`isError: true`), and read tools carry `readOnlyHint` annotations so clients
can auto-approve them.

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

## Further reading

- [API behaviour worth knowing](docs/api-behaviour.md) — measured quirks of the
  live API: `*Pec` scaling, why power samples can't be summed into energy,
  timezones and daylight saving, rate limits, and where the spec is wrong.
- [Running with Docker](docs/docker.md) — 1Password, rebuilding, Apple `container`.
- [Development](docs/development.md) — API spec version, request signing, tests,
  known gaps.

## License

[MIT](LICENSE) © 2026 Stefan Caspersz
