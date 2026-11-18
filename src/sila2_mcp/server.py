"""MCP server: SiLA 2 properties and commands as tools.

Read-only tools (list_servers, describe_server, get_property) never move hardware.
call_command can: keep it behind a confirmation in the MCP client's permissions.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import sila

mcp = MCPServer(
    "sila2",
    instructions=(
        "Tools for SiLA 2 lab instruments. list_servers, describe_server and get_property are "
        "read-only. call_command may move hardware: confirm with the user before each motion. "
        "To learn a server, call describe_server with brief=true first, then with feature=... for "
        "the parameters and types of the feature you need."
    ),
)
_pool: sila.Pool | None = None


def _get_pool() -> sila.Pool:
    if _pool is None:
        raise RuntimeError("server not configured")
    return _pool


def _list_servers() -> list[dict[str, Any]]:
    pool = _get_pool()
    out = []
    for name, cfg in pool.servers.items():
        entry: dict[str, Any] = {
            "server": name,
            "address": f"{cfg.host}:{cfg.port}",
            "tls": not cfg.insecure,
            "description": cfg.description,
        }
        try:
            client = pool.client(name)
            entry["connected"] = True
            entry["features"] = sorted(client._features)
        except sila.SilaError as e:
            entry["connected"] = False
            entry["error"] = str(e)
        out.append(entry)
    return out


# The sila2 client blocks (gRPC calls, waiting for observable commands), so every tool runs
# in a worker thread and the MCP event loop stays responsive during long commands.


async def _run(fn, *args):
    """Run `fn` in a worker thread; a SilaError reaches the client with its message.

    MCPServer reports any other exception as a bare "Error executing tool" and keeps the
    text on the server, so the client could not tell e.g. an unknown server from a crash.
    """
    try:
        return await anyio.to_thread.run_sync(fn, *args)
    except sila.SilaError as e:
        raise ToolError(str(e)) from e


@mcp.tool()
async def list_servers() -> list[dict[str, Any]]:
    """List the configured SiLA 2 servers and whether each one answers right now.

    Read-only. Tries to connect to each server; a server that does not answer is reported
    with its error instead of failing the whole call.
    """
    return await _run(_list_servers)


@mcp.tool()
async def describe_server(
    server: str,
    feature: str | None = None,
    brief: bool = False,
    include_standard: bool = False,
) -> dict[str, Any]:
    """Describe one server's features: commands (observable or not, parameters, responses,
    intermediate responses, defined errors), properties, custom data types (a type named in a
    parameter or response, e.g. "StreamName", is defined under `data_types`) and defined
    execution errors, with their FDL descriptions.

    Read-only. Use it to learn parameter names and types before call_command.
    The full description can be long: `brief=true` lists only each feature's command and
    property names with a one-sentence summary; `feature` returns just that feature.
    The SiLAService feature every server implements is left out unless `include_standard`.
    """
    return await _run(sila.describe, _get_pool(), server, feature, brief, include_standard)


@mcp.tool()
async def get_property(server: str, property: str, feature: str | None = None) -> dict[str, Any]:
    """Read a property's current value once (observable properties too).

    Read-only. `feature` is needed only when two features of the server share the property name.
    A Binary value is written to a file, as described for call_command.
    """
    return await _run(sila.get_property, _get_pool(), server, property, feature)


@mcp.tool()
async def call_command(
    server: str,
    command: str,
    parameters: dict[str, Any] | None = None,
    feature: str | None = None,
    timeout_s: float = 600.0,
) -> dict[str, Any]:
    """Run a SiLA 2 command and return its responses. MAY MOVE HARDWARE.

    `parameters` maps parameter identifiers to values, e.g. {"BucketNumber": 1}.
    An observable command is waited for until it finishes or `timeout_s` passes; its
    intermediate responses are collected and returned. On failure, `ok` is false and
    `error` holds the SiLA error (defined execution error identifier and message).
    `feature` is needed only when two features of the server share the command name.
    Binary responses (images, videos, data files) are written to local files; the result holds
    {"file", "content_type", "length"} instead of the bytes, so nothing of the content reaches
    the model unless the file is opened afterwards (e.g. reading an image, which sends it to the
    model like any other image in the conversation). Files are kept until deleted by hand;
    sila2-mcp never removes them.
    """
    return await _run(sila.call_command, _get_pool(), server, command, parameters, feature, timeout_s)


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP server for SiLA 2 servers")
    parser.add_argument("--config", help="servers.toml (default: $SILA2_MCP_CONFIG or ./servers.toml)")
    parser.add_argument(
        "--output-dir",
        help="where Binary responses are written (default: $SILA2_MCP_OUTPUT_DIR, output_dir in the "
        "config, or %%LOCALAPPDATA%%/sila2-mcp/files)",
    )
    args = parser.parse_args()

    # stdout carries the MCP protocol; everything else goes to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

    global _pool
    config = sila.load_config(args.config, args.output_dir)
    _pool = sila.Pool(config.servers, config.output_dir, config.path)
    mcp.run()


if __name__ == "__main__":
    main()
