# sila2-mcp

An MCP server that exposes SiLA 2 servers as tools, so an MCP client (e.g. Claude Code)
can read properties and send commands directly instead of writing a client script each time.

It is generic: the servers are listed in `servers.toml`, and each server's features are
discovered from the server itself (`sila2` Python client), so no per-instrument code is needed.

## Tools

| Tool | Moves hardware? | What it does |
|---|---|---|
| `list_servers` | no | The configured servers and whether each answers now |
| `describe_server(server, include_standard?)` | no | Features, commands (observable or not, parameters, responses, errors), properties, custom data types and defined errors; `SiLAService` only with `include_standard` |
| `get_property(server, property, feature?)` | no | Read a property once |
| `call_command(server, command, parameters?, feature?, timeout_s?)` | **may** | Run a command; an observable one is waited for and its intermediate responses collected |

Results are JSON. On failure `ok` is `false` and `error` holds the SiLA error
(e.g. a defined execution error's identifier and message). An unknown server, feature,
command or property, or a server that cannot be reached, is reported as a tool error with
its reason.

Binary values (images, videos, data files) are written to the output directory and
appear in the result as `{"file": ..., "content_type": ..., "length": ...}`, so they do not
enter the client's context. The content type comes from the FDL `ContentType` constraint,
otherwise from the file's leading bytes.

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

Binary responses go to `--output-dir`, else `$SILA2_MCP_OUTPUT_DIR`, else a top-level
`output_dir = "..."` in the config, else `%LOCALAPPDATA%\sila2-mcp\files`
(`~/.local/share/sila2-mcp/files` elsewhere). Files are not deleted automatically.

## Use with Claude Code

```sh
claude mcp add sila2 -- uv --directory /path/to/sila2-mcp run sila2-mcp
```

The server talks MCP over stdio. Connections to the SiLA 2 servers are opened on first use
and kept; after a SiLA 2 server restarts, the next failing call drops the connection and the
following call reconnects.

## License

MIT License. Copyright (c) 2026 RIKEN. See [LICENSE](LICENSE).
