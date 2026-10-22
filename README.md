# sila2-mcp

An MCP server that exposes SiLA 2 servers as tools, so an MCP client (e.g. Claude Code)
can read properties and send commands directly instead of writing a client script each time.

It is generic: the servers are listed in `servers.toml`, and each server's features are
discovered from the server itself (`sila2` Python client), so no per-instrument code is needed.

## Tools

| Tool | Moves hardware? | What it does |
|---|---|---|
| `list_servers` | no | The configured servers and whether each answers now |
| `describe_server(server)` | no | Features, commands (observable or not, parameters, responses, errors) and properties |
| `get_property(server, property, feature?)` | no | Read a property once |
| `call_command(server, command, parameters?, feature?, timeout_s?)` | **may** | Run a command; an observable one is waited for and its intermediate responses collected |

Results are JSON. On failure `ok` is `false` and `error` holds the SiLA error
(e.g. a defined execution error's identifier and message).

Keep `call_command` behind a confirmation in your MCP client's permissions and allow
the read-only tools.

## Setup

```sh
uv sync
```

Edit `servers.toml` (or point `SILA2_MCP_CONFIG` / `--config` at another file):

```toml
[servers.atc]
host = "127.0.0.1"
port = 5140
insecure = true        # no TLS; for TLS set false and optionally root_cert = "path/to/ca.pem"
description = "Thermal cycler"
```

## Use with Claude Code

```sh
claude mcp add sila2 -- uv --directory /path/to/sila2-mcp run sila2-mcp
```

The server talks MCP over stdio. Connections to the SiLA 2 servers are opened on first use
and kept; after a SiLA 2 server restarts, the next failing call drops the connection and the
following call reconnects.

## License

MIT. See [LICENSE](LICENSE).
