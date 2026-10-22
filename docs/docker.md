# Running with Docker

The `Dockerfile` builds a slim image that serves the same HTTP app on port
8765. `.env` is excluded by `.dockerignore`, so credentials are never baked
into the image — pass them at run time:

```bash
docker build -t soliscloud-mcp-server:latest .
docker run -d --name soliscloud-mcp-server --env-file .env -p 127.0.0.1:8765:8765 soliscloud-mcp-server:latest
```

The MCP endpoint is then `http://127.0.0.1:8765/mcp`. The server has no
authentication of its own, and anyone who can reach it acts with your API key
(including the write tools, if `SOLIS_ENABLE_WRITES=1`), so keep the port bound
to `127.0.0.1` rather than publishing it on all interfaces.

## 1Password references

If your `.env` holds 1Password `op://` references instead of literal values,
resolve them with `op run` and forward the variables by name:

```bash
op run --env-file=.env -- docker run -d --name soliscloud-mcp-server \
  -e SOLIS_KEY_ID -e SOLIS_KEY_SECRET -e SOLIS_API_BASE_URL \
  -p 127.0.0.1:8765:8765 soliscloud-mcp-server:latest
```

Add `-e SOLIS_TOOLSETS` / `-e SOLIS_ENABLE_WRITES` the same way if you set them.

## Monitoring and rebuilding

Check on it with `docker logs soliscloud-mcp-server` and `docker stats soliscloud-mcp-server`. After
changing the code, rebuild and recreate the container:

```bash
docker rm -f soliscloud-mcp-server && docker build -t soliscloud-mcp-server:latest .
```

then run it again as above.

## Apple `container` (macOS)

The same image works with Apple's
[`container`](https://github.com/apple/container) CLI — the commands mirror
Docker's (`container build`, `container run -e ... -d`, `container logs`,
`container stats`). It gives the container its own address instead of
publishing a port: find it with `container ls` and use
`http://<container-ip>:8765/mcp`. The address can change when the container is
recreated, so update your client config if it stops connecting.
