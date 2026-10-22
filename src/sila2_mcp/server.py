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

from . import sila

mcp = MCPServer(
    "sila2",
    instructions=(
        "Tools for SiLA 2 lab instruments. list_servers, describe_server and get_property are "
        "read-only. call_command may move hardware: confirm with the user before each motion."
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


@mcp.tool()
async def list_servers() -> list[dict[str, Any]]:
    """List the configured SiLA 2 servers and whether each one answers right now.

    Read-only. Tries to connect to each server; a server that does not answer is reported
    with its error instead of failing the whole call.
    """
    return await anyio.to_thread.run_sync(_list_servers)


@mcp.tool()
async def describe_server(server: str) -> dict[str, Any]:
    """Describe one server's features: commands (observable or not, parameters, responses,
    intermediate responses, defined errors) and properties, with their FDL descriptions.

    Read-only. Use it to learn parameter names and types before call_command.
    """
    return await anyio.to_thread.run_sync(sila.describe, _get_pool(), server)


@mcp.tool()
async def get_property(server: str, property: str, feature: str | None = None) -> dict[str, Any]:
    """Read a property's current value once (observable properties too).

    Read-only. `feature` is needed only when two features of the server share the property name.
    """
    return await anyio.to_thread.run_sync(sila.get_property, _get_pool(), server, property, feature)


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
    """
    return await anyio.to_thread.run_sync(
        sila.call_command, _get_pool(), server, command, parameters, feature, timeout_s
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP server for SiLA 2 servers")
    parser.add_argument("--config", help="servers.toml (default: $SILA2_MCP_CONFIG or ./servers.toml)")
    args = parser.parse_args()

    # stdout carries the MCP protocol; everything else goes to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

    global _pool
    _pool = sila.Pool(sila.load_config(args.config))
    mcp.run()


if __name__ == "__main__":
    main()
