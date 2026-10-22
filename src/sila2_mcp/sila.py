"""SiLA 2 side: connections to the configured servers, feature introspection, and calls.

Kept free of MCP so it can be used and tested on its own.
"""

from __future__ import annotations

import base64
import datetime
import decimal
import enum
import os
import threading
import time
import tomllib
import uuid
import warnings
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sila2.client import SilaClient

# The sila2 client warns on every observable command whose lifetime the server shortens.
warnings.filterwarnings("ignore", message="Server shortened the lifetime")

# Features every SiLA 2 server implements; skipped when looking a command up by name.
_STANDARD_FEATURES = {"SiLAService"}


@dataclass(frozen=True)
class ServerConfig:
    name: str
    host: str
    port: int
    insecure: bool = True
    root_cert: str | None = None
    description: str = ""


def load_config(path: str | os.PathLike | None = None) -> dict[str, ServerConfig]:
    """Read the server table from `path`, $SILA2_MCP_CONFIG, or ./servers.toml."""
    path = Path(path or os.environ.get("SILA2_MCP_CONFIG") or "servers.toml")
    with open(path, "rb") as f:
        data = tomllib.load(f)
    servers = {}
    for name, entry in data.get("servers", {}).items():
        servers[name] = ServerConfig(
            name=name,
            host=entry["host"],
            port=int(entry["port"]),
            insecure=bool(entry.get("insecure", True)),
            root_cert=entry.get("root_cert"),
            description=entry.get("description", ""),
        )
    return servers


class SilaError(Exception):
    """An error to report to the MCP client as-is."""


class Pool:
    """One SilaClient per configured server, connected on first use and reconnected after a failure."""

    def __init__(self, servers: dict[str, ServerConfig]):
        self.servers = servers
        self._clients: dict[str, SilaClient] = {}
        self._lock = threading.Lock()

    def client(self, server: str) -> SilaClient:
        if server not in self.servers:
            raise SilaError(f"unknown server {server!r}; configured: {sorted(self.servers)}")
        with self._lock:
            if server not in self._clients:
                cfg = self.servers[server]
                kwargs: dict[str, Any] = {"insecure": True} if cfg.insecure else {}
                if not cfg.insecure and cfg.root_cert:
                    kwargs["root_certs"] = Path(cfg.root_cert).read_bytes()
                try:
                    self._clients[server] = SilaClient(cfg.host, cfg.port, **kwargs)
                except Exception as e:
                    raise SilaError(f"cannot connect to {server} at {cfg.host}:{cfg.port}: {type(e).__name__}: {e}") from e
            return self._clients[server]

    def drop(self, server: str) -> None:
        """Forget a client so the next call reconnects (e.g. after the server restarted)."""
        with self._lock:
            client = self._clients.pop(server, None)
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


# --- introspection -------------------------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(node: ET.Element, name: str) -> ET.Element | None:
    for c in node:
        if _local(c.tag) == name:
            return c
    return None


def _children(node: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in node if _local(c.tag) == name]


def _text(node: ET.Element | None, name: str) -> str:
    c = None if node is None else _child(node, name)
    return " ".join((c.text or "").split()) if c is not None else ""


def _type_str(data_type: ET.Element | None) -> str:
    """A compact, readable rendering of an FDL <DataType>."""
    if data_type is None:
        return "?"
    inner = next(iter(data_type), None)
    if inner is None:
        return "?"
    kind = _local(inner.tag)
    if kind == "Basic":
        return (inner.text or "").strip()
    if kind == "DataTypeIdentifier":
        return (inner.text or "").strip()
    if kind == "List":
        return f"List[{_type_str(_child(inner, 'DataType'))}]"
    if kind == "Constrained":
        base = _type_str(_child(inner, "DataType"))
        constraints = _child(inner, "Constraints")
        parts = []
        if constraints is not None:
            for c in constraints:
                values = [v.text for v in c if v.text] if len(c) else []
                parts.append(f"{_local(c.tag)}={'|'.join(values) if values else (c.text or '').strip()}")
        return f"{base} ({', '.join(parts)})" if parts else base
    if kind == "Structure":
        fields = [f"{_text(e, 'Identifier')}: {_type_str(_child(e, 'DataType'))}" for e in _children(inner, "Element")]
        return "Structure{" + ", ".join(fields) + "}"
    return kind


def _elements(node: ET.Element, name: str) -> list[dict[str, str]]:
    return [
        {
            "identifier": _text(e, "Identifier"),
            "type": _type_str(_child(e, "DataType")),
            "description": _text(e, "Description"),
        }
        for e in _children(node, name)
    ]


def _error_ids(command: ET.Element) -> list[str]:
    errors = _child(command, "DefinedExecutionErrors")
    return [] if errors is None else [(e.text or "").strip() for e in errors]


def describe(pool: Pool, server: str) -> dict[str, Any]:
    client = pool.client(server)
    features = []
    for identifier, feature in client._features.items():
        root = ET.fromstring(feature._feature_definition)
        commands = []
        for cmd in _children(root, "Command"):
            commands.append(
                {
                    "identifier": _text(cmd, "Identifier"),
                    "observable": _text(cmd, "Observable") == "Yes",
                    "description": _text(cmd, "Description"),
                    "parameters": _elements(cmd, "Parameter"),
                    "responses": _elements(cmd, "Response"),
                    "intermediate_responses": _elements(cmd, "IntermediateResponse"),
                    "errors": _error_ids(cmd),
                }
            )
        properties = [
            {
                "identifier": _text(p, "Identifier"),
                "observable": _text(p, "Observable") == "Yes",
                "type": _type_str(_child(p, "DataType")),
                "description": _text(p, "Description"),
            }
            for p in _children(root, "Property")
        ]
        features.append(
            {
                "identifier": identifier,
                "description": _text(root, "Description"),
                "commands": commands,
                "properties": properties,
            }
        )
    return {"server": server, "features": features}


# --- values ----------------------------------------------------------------------------------------


def to_jsonable(value: Any) -> Any:
    """Turn sila2 client values (namedtuples, bytes, dates, ...) into JSON-compatible ones."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, tuple) and hasattr(value, "_asdict"):
        return {k: to_jsonable(v) for k, v in value._asdict().items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (bytes, bytearray)):
        return {"base64": base64.b64encode(value).decode("ascii"), "length": len(value)}
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return value.total_seconds()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, enum.Enum):
        return value.name
    return str(value)


def _error(e: BaseException) -> dict[str, Any]:
    info: dict[str, Any] = {"type": type(e).__name__, "message": str(e)}
    for attr in ("identifier", "fully_qualified_identifier", "error_type"):
        if hasattr(e, attr):
            info[attr] = to_jsonable(getattr(e, attr))
    return info


# --- lookups ---------------------------------------------------------------------------------------


def _find(pool: Pool, server: str, feature: str | None, member: str, kind: str):
    """Return (feature identifier, client attribute) for a command or property name."""
    client = pool.client(server)
    if feature:
        if feature not in client._features:
            raise SilaError(f"{server} has no feature {feature!r}; features: {sorted(client._features)}")
        candidates = [feature]
    else:
        candidates = [f for f in client._features if f not in _STANDARD_FEATURES] + [
            f for f in client._features if f in _STANDARD_FEATURES
        ]
    matches = []
    for fid in candidates:
        root = ET.fromstring(client._features[fid]._feature_definition)
        if any(_text(n, "Identifier") == member for n in _children(root, kind)):
            matches.append(fid)
    if not matches:
        raise SilaError(f"{server} has no {kind.lower()} {member!r}" + (f" in {feature}" if feature else ""))
    non_standard = [m for m in matches if m not in _STANDARD_FEATURES]
    if not feature and len(non_standard) > 1:
        raise SilaError(f"{kind.lower()} {member!r} exists in several features {non_standard}; pass feature")
    fid = (non_standard or matches)[0]
    return fid, getattr(getattr(client, fid), member)


# --- calls -----------------------------------------------------------------------------------------


def get_property(pool: Pool, server: str, prop: str, feature: str | None = None) -> dict[str, Any]:
    try:
        fid, attr = _find(pool, server, feature, prop, "Property")
        value = attr.get()
    except SilaError:
        raise
    except Exception as e:
        pool.drop(server)
        return {"ok": False, "server": server, "property": prop, "error": _error(e)}
    return {"ok": True, "server": server, "feature": fid, "property": prop, "value": to_jsonable(value)}


def call_command(
    pool: Pool,
    server: str,
    command: str,
    parameters: dict[str, Any] | None = None,
    feature: str | None = None,
    timeout_s: float = 600.0,
) -> dict[str, Any]:
    """Run a command; for an observable one, wait until it finishes (or `timeout_s` passes)."""
    parameters = parameters or {}
    fid, attr = _find(pool, server, feature, command, "Command")
    base = {"server": server, "feature": fid, "command": command, "parameters": parameters}
    started = time.monotonic()
    try:
        result = attr(**parameters)
    except Exception as e:
        return {**base, "ok": False, "seconds": round(time.monotonic() - started, 1), "error": _error(e)}

    if not hasattr(result, "execution_uuid"):  # unobservable: the call returned the responses
        return {**base, "ok": True, "seconds": round(time.monotonic() - started, 1), "responses": to_jsonable(result)}

    instance = result
    intermediate: list[Any] = []
    subscription = None
    try:
        subscription = instance.subscribe_to_intermediate_responses()
        subscription.add_callback(lambda item: intermediate.append(to_jsonable(item)))
    except Exception:
        subscription = None  # the command has no intermediate responses

    while not instance.done:
        if time.monotonic() - started > timeout_s:
            if subscription is not None:
                subscription.cancel()
            return {
                **base,
                "ok": False,
                "timed_out": True,
                "seconds": round(time.monotonic() - started, 1),
                "execution_uuid": str(instance.execution_uuid),
                "status": to_jsonable(instance.status),
                "intermediate_responses": intermediate,
                "error": {"type": "Timeout", "message": f"still running after {timeout_s:.0f} s"},
            }
        time.sleep(0.2)
    if subscription is not None:
        subscription.cancel()

    seconds = round(time.monotonic() - started, 1)
    try:
        responses = instance.get_responses()
    except Exception as e:
        return {**base, "ok": False, "seconds": seconds, "intermediate_responses": intermediate, "error": _error(e)}
    return {
        **base,
        "ok": True,
        "seconds": seconds,
        "responses": to_jsonable(responses),
        "intermediate_responses": intermediate,
    }
