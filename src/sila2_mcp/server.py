"""MCP server: SiLA 2 properties and commands as tools.

list_servers and describe_server never touch an instrument. call_command (and get_property,
if so configured) asks the user for approval through MCP elicitation, according to the
confirmation policy in servers.toml; the SiLA call runs only after an approval.
"""

from __future__ import annotations

import argparse
import functools
import json
import logging
import sys
import time
from dataclasses import dataclass
from typing import Annotated, Any, Literal

import anyio
from mcp.server.mcpserver import (
    AcceptedElicitation,
    Context,
    Elicit,
    ElicitationResult,
    MCPServer,
    Resolve,
)
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, create_model

from . import approval, sila

mcp = MCPServer(
    "sila2",
    instructions=(
        "Tools for SiLA 2 lab instruments. list_servers and describe_server only read "
        "descriptions. call_command may move hardware or change an instrument's state; sila2-mcp "
        "itself asks the user to approve calls according to its confirmation policy (shown by "
        "describe_server), so you need not ask again before calling. If a call was not approved, "
        "the result has error.type NotApproved: do not retry it on your own; tell the user and ask "
        "how to proceed. "
        "To learn a server, call describe_server with brief=true first, then with feature=... for "
        "the parameters and types of the feature you need."
    ),
)
_pool: sila.Pool | None = None
_approvals = approval.Approvals()
_audit = approval.AuditLog(None)


def _get_pool() -> sila.Pool:
    if _pool is None:
        raise RuntimeError("server not configured")
    return _pool


# --- confirmation --------------------------------------------------------------------------------
#
# A Resolve(...) parameter runs its resolver before the tool body. The resolver either lets the
# call through (a Gate) or returns Elicit(...), which the SDK turns into a question to the user:
# an InputRequiredResult on 2026-07-28+ connections (the client retries the call with the answer;
# the answer travels in a sealed request_state the client cannot forge), or an elicitation/create
# request on older ones. The tool body, and so the SiLA call, runs only after that.


def _ttl_text(ttl_minutes: float | None) -> str:
    return "until the MCP server restarts" if ttl_minutes is None else f"for {ttl_minutes:g} minutes"


# The forms carry no docstrings: pydantic would put them into the schema shown to the user.


@functools.cache
def _every_form(label: str) -> type[BaseModel]:
    # "every" mode: no fields, the user only accepts or declines
    return create_model("Approval", __config__=ConfigDict(title=label))


@functools.cache
def _once_form(ttl_minutes: float | None, kind: str, label: str) -> type[BaseModel]:
    # "once" mode: Accept with "this time only" (ask again next time) or "remember" (no more
    # questions for this command for the approval period); Decline refuses. "This time only" is
    # preselected so that nothing is remembered by accident.
    choices = [
        {"const": "this_time", "title": "This time only"},
        {"const": "remember", "title": f"This {kind} {_ttl_text(ttl_minutes)}"},
    ]

    def titled(schema: dict[str, Any]) -> None:  # a titled single-select enum, as the spec defines it
        schema.pop("enum", None)
        schema["oneOf"] = choices

    return create_model(
        "OnceApproval",
        allow=(Literal["this_time", "remember"], Field("this_time", title=label, json_schema_extra=titled)),
    )


@dataclass(frozen=True)
class Gate:
    """A resolver outcome that needs no question to the user."""

    status: str  # "pass" (no confirmation needed / remembered), "unsupported", "failed"
    reason: str = ""


def _elicitation_supported(ctx: Context) -> bool:
    caps = ctx.client_capabilities
    elicitation = caps.elicitation if caps is not None else None
    # same test as the SDK: bare `elicitation: {}` means form support; url-only does not
    return elicitation is not None and (elicitation.form is not None or elicitation.url is None)


_VALUE_CHARS = 40  # a longer parameter value is shortened in the first line of the question


def _call_text(member: str, parameters: dict[str, Any] | None) -> tuple[str, bool]:
    """'MoveRelative(DeltaPan=30, DeltaTilt=0)', and whether a value had to be shortened."""
    parts, shortened = [], False
    for k, v in (parameters or {}).items():
        text = json.dumps(v, ensure_ascii=False)
        if len(text) > _VALUE_CHARS:
            text, shortened = text[: _VALUE_CHARS - 3] + "...", True
        parts.append(f"{k}={text}")
    return f"{member}({', '.join(parts)})", shortened


async def _gate(
    ctx: Context, kind: approval.Kind, server: str, member: str, feature: str | None, parameters: dict[str, Any] | None
) -> Gate | Elicit[BaseModel]:
    pool = _get_pool()
    cfg = pool.servers.get(server)
    if cfg is None:
        return Gate("pass")  # unknown server: the body reports it; nothing can run
    policy = cfg.policy
    if policy.mode(kind) == "none":
        return Gate("pass")
    try:
        fid, summary = await anyio.to_thread.run_sync(
            sila.member_info, pool, server, "Command" if kind == "command" else "Property", member, feature
        )
    except sila.SilaError as e:
        return Gate("failed", str(e))  # fail closed: without a lookup there is no approval
    key = (kind, server, fid, member)
    if not approval.needs_approval(policy, _approvals, key):
        return Gate("pass")
    if not _elicitation_supported(ctx):
        return Gate(
            "unsupported",
            f"approval required (confirm.{'commands' if kind == 'command' else 'properties'} = "
            f"{policy.mode(kind)!r} for {server}), but this MCP client cannot ask the user (no form "
            f"elicitation support). If {server} needs no confirmation, set confirm = {{"
            f"{'commands' if kind == 'command' else 'properties'} = \"none\"}} for it in "
            f"{pool.config_path or 'servers.toml'}.",
        )
    # Clients may show only the first few lines (Claude Code: "+N more lines"), so the first line
    # names the call completely and there are no blank lines.
    shortened = False
    if kind == "command":
        call, shortened = _call_text(member, parameters)
        lines = [f"Run {call} on {server}?"]
    else:
        lines = [f"Read {member} on {server}?"]
    lines.append(f"{fid}: {summary}" if summary else f"Feature: {fid}")
    lines.append(f"Server: {server} ({cfg.host}:{cfg.port})" + (f" - {cfg.description}" if cfg.description else ""))
    if shortened:
        lines.append("Parameters: " + json.dumps(parameters, ensure_ascii=False))
    label = f"Allow {member} on {server}"
    if policy.mode(kind) == "once":
        return Elicit("\n".join(lines), _once_form(policy.ttl_minutes, kind, label))
    return Elicit("\n".join(lines), _every_form(label))


async def _approve_command(
    ctx: Context, server: str, command: str, parameters: dict[str, Any] | None, feature: str | None
) -> Gate | Elicit[BaseModel]:
    return await _gate(ctx, "command", server, command, feature, parameters)


async def _approve_property(
    ctx: Context, server: str, property: str, feature: str | None
) -> Gate | Elicit[BaseModel]:
    return await _gate(ctx, "property", server, property, feature, None)


async def _admit(
    outcome: ElicitationResult[Any],
    kind: approval.Kind,
    server: str,
    member: str,
    feature: str | None,
    parameters: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Act on the resolver's outcome: None lets the call run, a dict is the refusal to return."""
    base: dict[str, Any] = {"server": server, kind: member}
    if kind == "command":
        base["parameters"] = parameters or {}
    audit = {"server": server, "kind": kind, "member": member, "feature": feature, "parameters": parameters}
    if isinstance(outcome, AcceptedElicitation):
        data = outcome.data
        if isinstance(data, Gate):
            if data.status == "pass":
                return None
            if data.status == "failed":
                raise ToolError(data.reason)
            _audit.write("not_asked", reason=data.reason, **audit)
            return {
                **base,
                "ok": False,
                "error": {"type": "NotApproved", "reason": "cannot_ask", "message": data.reason},
            }
        # the user accepted
        pool = _get_pool()
        policy = pool.servers[server].policy
        fid, _ = await anyio.to_thread.run_sync(
            sila.member_info, pool, server, "Command" if kind == "command" else "Property", member, feature
        )
        # "once" mode: remember only when the user chose so; "this time only" leaves it asking
        scope = getattr(data, "allow", None) if policy.mode(kind) == "once" else None
        if scope == "remember":
            _approvals.grant((kind, server, fid, member))
        _audit.write("approved", mode=policy.mode(kind), scope=scope, **{**audit, "feature": fid})
        return None
    # A decline may come from a person or from a client that declines automatically (e.g. one
    # that cannot show the question); the protocol does not tell them apart.
    action = "declined" if outcome.action == "decline" else "dismissed"
    _audit.write(action, **audit)
    message = (
        "The request was declined (by the user, or automatically by an MCP client that cannot "
        "show the question); it was not run."
        if action == "declined"
        else "The request was dismissed without an answer; it was not run."
    )
    return {**base, "ok": False, "error": {"type": "NotApproved", "reason": action, "message": message}}


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
        entry["confirmation"] = cfg.policy.describe()
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
    `confirmation` gives the server's confirmation policy: for commands and for properties,
    "none" (never asks), "every" (asks the user each time) or "once" (asks once per command or
    property, then not again for approval_ttl_minutes).
    """
    result = await _run(sila.describe, _get_pool(), server, feature, brief, include_standard)
    return {"server": server, "confirmation": _get_pool().servers[server].policy.describe(), **result}


@mcp.tool()
async def get_property(
    server: str,
    property: str,
    feature: str | None = None,
    approval: Annotated[ElicitationResult[BaseModel], Resolve(_approve_property)] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Read a property's current value once (observable properties too).

    Reading never changes the instrument. Depending on the server's confirmation policy for
    properties (see describe_server; by default "none"), the user may be asked first.
    `feature` is needed only when two features of the server share the property name.
    A Binary value is written to a file, as described for call_command.
    """
    refusal = await _admit(approval, "property", server, property, feature, None)
    if refusal is not None:
        return refusal
    result = await _run(sila.get_property, _get_pool(), server, property, feature)
    if _get_pool().servers[server].policy.properties != "none":
        _audit.write("read", server=server, kind="property", member=property, ok=result.get("ok"))
    return result


@mcp.tool()
async def call_command(
    server: str,
    command: str,
    parameters: dict[str, Any] | None = None,
    feature: str | None = None,
    timeout_s: float = 600.0,
    approval: Annotated[ElicitationResult[BaseModel], Resolve(_approve_command)] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Run a SiLA 2 command and return its responses. MAY MOVE HARDWARE.

    Before running, sila2-mcp asks the user for approval according to the server's confirmation
    policy for commands (see describe_server; by default "once": once per command, then not
    again for a while). If the user declines, or the client cannot ask, nothing is run and the
    result has ok=false with error.type "NotApproved".
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
    refusal = await _admit(approval, "command", server, command, feature, parameters)
    if refusal is not None:
        return refusal
    started = time.monotonic()
    try:
        result = await _run(sila.call_command, _get_pool(), server, command, parameters, feature, timeout_s)
    except ToolError as e:
        _audit.write("executed", server=server, kind="command", member=command, feature=feature,
                     parameters=parameters, ok=False, error=str(e))
        raise
    _audit.write(
        "executed",
        server=server,
        kind="command",
        member=command,
        feature=result.get("feature", feature),
        parameters=parameters,
        ok=result.get("ok"),
        seconds=round(time.monotonic() - started, 1),
        error=(result.get("error") or {}).get("type"),
    )
    return result


@mcp.tool()
async def revoke_approvals(server: str | None = None) -> dict[str, Any]:
    """Forget the approvals given in "once" mode (for one server, or all), so the next call of
    each command asks the user again. Never grants anything.
    """
    n = _approvals.revoke(server)
    _audit.write("revoked", server=server, count=n)
    return {"revoked": n, "server": server}


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP server for SiLA 2 servers")
    parser.add_argument("--config", help="servers.toml (default: $SILA2_MCP_CONFIG or ./servers.toml)")
    parser.add_argument(
        "--output-dir",
        help="where Binary responses are written (default: $SILA2_MCP_OUTPUT_DIR, output_dir in the "
        "config, or %%LOCALAPPDATA%%/sila2-mcp/files)",
    )
    parser.add_argument(
        "--approval-ttl",
        help='minutes an approval in "once" mode is remembered, or "unlimited" '
        "(default: approval_ttl_minutes in [confirm], else 30)",
    )
    args = parser.parse_args()

    # stdout carries the MCP protocol; everything else goes to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

    global _pool, _audit
    try:
        config = sila.load_config(args.config, args.output_dir, args.approval_ttl)
    except ValueError as e:
        parser.error(str(e))
    _pool = sila.Pool(config.servers, config.output_dir, config.path)
    _audit = approval.AuditLog(config.audit_log)
    _audit.write(
        "started",
        config=str(config.path),
        confirmation={name: cfg.policy.describe() for name, cfg in config.servers.items()},
    )
    mcp.run()


if __name__ == "__main__":
    main()
