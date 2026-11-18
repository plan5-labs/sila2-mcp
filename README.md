# sila2-mcp

An MCP server that exposes SiLA 2 servers as tools, so an MCP client (e.g. Claude Code)
can read properties and send commands directly instead of writing a client script each time.

It is generic: the servers are listed in `servers.toml`, and each server's features are
discovered from the server itself (`sila2` Python client), so no per-instrument code is needed.

## Tools

| Tool | Moves hardware? | What it does |
|---|---|---|
| `list_servers` | no | The configured servers, their confirmation policy, and whether each answers now |
| `describe_server(server, feature?, brief?, include_standard?)` | no | Features, commands (observable or not, parameters, responses, errors), properties, custom data types and defined errors, and the confirmation policy. `brief` lists only names with a one-sentence summary; `feature` limits it to one feature; `SiLAService` only with `include_standard` |
| `get_property(server, property, feature?)` | no | Read a property once (asks first only if `confirm.properties` says so) |
| `call_command(server, command, parameters?, feature?, timeout_s?)` | **may** | Run a command after the user's approval (see below); an observable one is waited for and its intermediate responses collected |
| `revoke_approvals(server?)` | no | Forget the approvals remembered in `once` mode |

Results are JSON. On failure `ok` is `false` and `error` holds the SiLA error
(e.g. a defined execution error's identifier and message). An unknown server, feature,
command or property, or a server that cannot be reached, is reported as a tool error with
its reason.

Binary values (images, videos, data files) are written to the output directory and
appear in the result as `{"file": ..., "content_type": ..., "length": ...}`, so they do not
enter the client's context. The content type comes from the FDL `ContentType` constraint,
otherwise from the file's leading bytes. The content reaches the model only if the client
opens the file afterwards (e.g. reads an image).

## Confirmation

Commands act on the physical world, so sila2-mcp itself asks the user before running them,
through MCP elicitation (the client shows the question to the user; the model cannot answer
it). The SiLA call runs only after an approval.

| Mode | Behavior |
|---|---|
| `none` | Never ask |
| `every` | Ask each time |
| `once` | Ask with three choices: Decline; Accept "this time only" (asked again next time); Accept "this command for N minutes" (not asked again for that command for `approval_ttl_minutes`). "This time only" is preselected |

Defaults: commands `once`, property reads `none`, approvals kept for 30 minutes.

```toml
[confirm]
commands = "once"              # none | every | once
properties = "none"            # none | every | once
approval_ttl_minutes = 30      # or "unlimited" (until the MCP server restarts); --approval-ttl overrides

[servers.ardea]
# ...
confirm = { commands = "every" }   # per-server override of the modes
```

- Declined, dismissed, or not askable (the client has no form elicitation support): nothing
  is run and the result has `ok: false`, `error.type: "NotApproved"` and `error.reason`
  `declined` / `dismissed` / `cannot_ask`. A server that needs no confirmation (e.g. a
  simulator) must be set to `none` explicitly.
- The question puts the call in its first line (`Run MoveRelative(DeltaPan=30, DeltaTilt=0) on
  tapo?`), since clients may show only the first lines.
- Claude Code shows the question in the terminal. The Claude Code VS Code extension currently
  declines every elicitation automatically (a client-side issue), so there nothing runs with
  `once` / `every`; a decline cannot be told apart from a person's, hence `declined` means "by
  the user, or automatically by the client". See issue #4 for per-client policies.
- Approvals live in the MCP server process: one session; restarting or reconnecting the server
  forgets them, and so does `revoke_approvals`. Nothing lets the model grant an approval.
- On MCP 2026-07-28 connections the question travels as an `InputRequiredResult` and the
  answer comes back in a `request_state` sealed by the server (AES-GCM, per-process key), so a
  client cannot forge an approval; older connections use `elicitation/create`.
- Whether a question really reaches a person is up to the MCP client. A client that answers
  elicitations automatically defeats the confirmation; sila2-mcp cannot tell.
- Every approval, refusal and executed command is appended to the audit log
  (`%LOCALAPPDATA%\sila2-mcp\audit.jsonl`, `~/.local/share/sila2-mcp/audit.jsonl` elsewhere;
  top-level `audit_log = "path"` or `false` in the config).
- This is a software confirmation. It does not replace the physical safety measures of the
  instruments (interlocks, light curtains, emergency stops).

When the client shows the questions, `call_command` can be allowed in the MCP client's own
permissions, so the user is not asked twice. Do this only after checking that the questions
appear (see below).

### Clients that cannot show the question (e.g. the Claude Code VS Code extension)

Signs: every command returns `NotApproved` with `reason: "declined"` although nobody declined
(no dialog appeared; the audit log shows `declined` within milliseconds of the call), or
`reason: "cannot_ask"`.

1. **Preferably, use a client that shows them.** Claude Code in a terminal does, including the
   integrated terminal of VS Code: run `claude` there instead of using the extension's chat.
2. **Otherwise, let the client's permission prompt be the confirmation.**
   - Use a configuration that does not ask, e.g. a copy of `servers.toml` with
     ```toml
     [confirm]
     commands = "none"
     properties = "none"
     ```
     and register sila2-mcp with it:
     ```sh
     claude mcp add -s user sila2 -- uv --directory /path/to/sila2-mcp run sila2-mcp --config /path/to/servers-noconfirm.toml
     ```
   - Keep `mcp__sila2__call_command` out of `permissions.allow` (and, to be explicit, list it
     in `permissions.ask`), so that Claude Code asks before every command; allow only the
     reading tools:
     ```json
     {
       "permissions": {
         "allow": ["mcp__sila2__list_servers", "mcp__sila2__describe_server", "mcp__sila2__get_property"],
         "ask": ["mcp__sila2__call_command"]
       }
     }
     ```
   - Do not use permission modes that may run tool calls without asking (e.g. auto mode,
     bypassPermissions) while instruments are connected: commands would then run with nobody
     asked.
   - What changes: the client asks once per call of `call_command`, for every command
     alike (no `once`, no "this time only"), showing the tool arguments (server, command,
     parameters). The audit log still records every executed command.
   - The registration (`-s user`) is shared by all Claude Code clients of the user, so the
     terminal then does not ask either. Switch back to the normal configuration when you use the
     terminal, or register the no-confirmation configuration with `-s local` in one project
     directory only (a local registration of the same name takes precedence there, for every
     client opened in that directory). Per-client policies are planned in issue #4.

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

Use it from Claude Code in a terminal, where the approval questions are shown (see
[Clients that cannot show the question](#clients-that-cannot-show-the-question-eg-the-claude-code-vs-code-extension)
for the VS Code extension).

The server talks MCP over stdio. Connections to the SiLA 2 servers are opened on first use
and kept; after a SiLA 2 server restarts, the next failing call drops the connection and the
following call reconnects.

## License

MIT License. Copyright (c) 2026 RIKEN. See [LICENSE](LICENSE).
