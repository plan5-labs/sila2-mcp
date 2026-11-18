"""SiLA 2 side: connections to the configured servers, feature introspection, and calls.

Kept free of MCP so it can be used and tested on its own.
"""

from __future__ import annotations

import base64
import datetime
import decimal
import enum
import mimetypes
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


def default_output_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return Path(base) / "sila2-mcp" / "files"


@dataclass(frozen=True)
class Config:
    servers: dict[str, ServerConfig]
    output_dir: Path  # where Binary values from the servers are written


def load_config(path: str | os.PathLike | None = None, output_dir: str | os.PathLike | None = None) -> Config:
    """Read the configuration from `path`, $SILA2_MCP_CONFIG, or ./servers.toml.

    The output directory is `output_dir`, else $SILA2_MCP_OUTPUT_DIR, else the file's top-level
    `output_dir`, else default_output_dir().
    """
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
    out = output_dir or os.environ.get("SILA2_MCP_OUTPUT_DIR") or data.get("output_dir")
    return Config(servers=servers, output_dir=Path(out).expanduser() if out else default_output_dir())


class SilaError(Exception):
    """An error to report to the MCP client as-is."""


class Pool:
    """One SilaClient per configured server, connected on first use and reconnected after a failure."""

    def __init__(self, servers: dict[str, ServerConfig], output_dir: str | os.PathLike | None = None):
        self.servers = servers
        self.output_dir = Path(output_dir) if output_dir else default_output_dir()
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


def _constraint_str(c: ET.Element) -> str:
    kind = _local(c.tag)
    if kind == "ContentType":
        return f"ContentType={_text(c, 'Type')}/{_text(c, 'Subtype')}"
    if kind == "Unit":
        return f"Unit={_text(c, 'Label')}"
    if len(c):
        return f"{kind}={'|'.join((v.text or '').strip() for v in c if (v.text or '').strip())}"
    return f"{kind}={(c.text or '').strip()}"


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
            parts = [_constraint_str(c) for c in constraints]
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


def _data_types(root: ET.Element) -> list[dict[str, Any]]:
    out = []
    for d in _children(root, "DataTypeDefinition"):
        data_type = _child(d, "DataType")
        entry: dict[str, Any] = {
            "identifier": _text(d, "Identifier"),
            "type": _type_str(data_type),
            "description": _text(d, "Description"),
        }
        structure = None if data_type is None else _child(data_type, "Structure")
        if structure is not None:  # the fields carry their descriptions; no need for the inline form
            entry["type"] = "Structure"
            entry["fields"] = _elements(structure, "Element")
        out.append(entry)
    return out


def describe(pool: Pool, server: str, include_standard: bool = False) -> dict[str, Any]:
    """Features with their commands, properties, data types and errors.

    The SiLAService feature, which every server implements, is left out unless `include_standard`.
    """
    client = pool.client(server)
    features = []
    for identifier, feature in client._features.items():
        if identifier in _STANDARD_FEATURES and not include_standard:
            continue
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
                "data_types": _data_types(root),
                "errors": [
                    {"identifier": _text(e, "Identifier"), "description": _text(e, "Description")}
                    for e in _children(root, "DefinedExecutionError")
                ],
            }
        )
    return {"server": server, "features": features}


# --- values ----------------------------------------------------------------------------------------


_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/tiff": ".tif",
    "video/mp4": ".mp4",
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "application/xml": ".xml",
    "application/json": ".json",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "application/octet-stream": ".bin",
}


def _sniff(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if data[4:8] == b"ftyp":
        return "video/mp4"
    if data.startswith(b"%PDF"):
        return "application/pdf"
    if data.startswith(b"PK\x03\x04"):
        return "application/zip"
    return "application/octet-stream"


class BinarySink:
    """Writes Binary values to files so a result carries a path instead of the bytes.

    `hints` maps a response (or property) identifier to the content type its FDL declares;
    other binaries are typed by their leading bytes.
    """

    def __init__(self, directory: Path, prefix: str, hints: dict[str, str] | None = None):
        self.directory = directory
        self.prefix = prefix
        self.hints = hints or {}

    def __call__(self, data: bytes, field: str | None) -> dict[str, Any]:
        content_type = self.hints.get(field or "") or _sniff(data)
        ext = _EXTENSIONS.get(content_type) or mimetypes.guess_extension(content_type) or ".bin"
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        name = "_".join(p for p in (self.prefix, stamp, field) if p)
        name = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in name) + ext
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / name
        path.write_bytes(data)
        return {"file": str(path), "content_type": content_type, "length": len(data)}


def to_jsonable(value: Any, sink: BinarySink | None = None, field: str | None = None) -> Any:
    """Turn sila2 client values (namedtuples, bytes, dates, ...) into JSON-compatible ones.

    Bytes go to `sink` (a file) when given, else into the result as base64.
    `field` is the identifier of the innermost named element, used as a content-type hint.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, tuple) and hasattr(value, "_asdict"):
        return {k: to_jsonable(v, sink, k) for k, v in value._asdict().items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v, sink, field) for v in value]
    if isinstance(value, dict):
        return {str(k): to_jsonable(v, sink, str(k)) for k, v in value.items()}
    if isinstance(value, (bytes, bytearray)):
        if sink is not None:
            return sink(bytes(value), field)
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
    """Return (feature identifier, client attribute, FDL root, FDL node) for a command or property name."""
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
    root = ET.fromstring(client._features[fid]._feature_definition)
    node = next(n for n in _children(root, kind) if _text(n, "Identifier") == member)
    return fid, getattr(getattr(client, fid), member), root, node


def _content_type(data_type: ET.Element | None, root: ET.Element, depth: int = 0) -> str | None:
    """The ContentType constraint of a (possibly constrained or named) Binary type, if any."""
    if data_type is None or depth > 10:
        return None
    inner = next(iter(data_type), None)
    if inner is None:
        return None
    kind = _local(inner.tag)
    if kind == "Constrained":
        constraints = _child(inner, "Constraints")
        c = None if constraints is None else _child(constraints, "ContentType")
        if c is not None:
            return f"{_text(c, 'Type')}/{_text(c, 'Subtype')}".lower()
        return _content_type(_child(inner, "DataType"), root, depth + 1)
    if kind == "DataTypeIdentifier":
        name = (inner.text or "").strip()
        for d in _children(root, "DataTypeDefinition"):
            if _text(d, "Identifier") == name:
                return _content_type(_child(d, "DataType"), root, depth + 1)
    return None


def _hints(root: ET.Element, node: ET.Element, element_kinds: tuple[str, ...]) -> dict[str, str]:
    """Identifier -> declared content type, for the Binary elements of a command."""
    hints = {}
    for kind in element_kinds:
        for e in _children(node, kind):
            ct = _content_type(_child(e, "DataType"), root)
            if ct:
                hints[_text(e, "Identifier")] = ct
    return hints


# --- calls -----------------------------------------------------------------------------------------


def get_property(pool: Pool, server: str, prop: str, feature: str | None = None) -> dict[str, Any]:
    try:
        fid, attr, root, node = _find(pool, server, feature, prop, "Property")
        value = attr.get()
    except SilaError:
        raise
    except Exception as e:
        pool.drop(server)
        return {"ok": False, "server": server, "property": prop, "error": _error(e)}
    ct = _content_type(_child(node, "DataType"), root)
    sink = BinarySink(pool.output_dir, f"{server}_{prop}", {prop: ct} if ct else None)
    return {"ok": True, "server": server, "feature": fid, "property": prop, "value": to_jsonable(value, sink, prop)}


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
    fid, attr, root, node = _find(pool, server, feature, command, "Command")
    sink = BinarySink(
        pool.output_dir, f"{server}_{command}", _hints(root, node, ("Response", "IntermediateResponse"))
    )
    base = {"server": server, "feature": fid, "command": command, "parameters": parameters}
    started = time.monotonic()
    try:
        result = attr(**parameters)
    except Exception as e:
        return {**base, "ok": False, "seconds": round(time.monotonic() - started, 1), "error": _error(e)}

    if not hasattr(result, "execution_uuid"):  # unobservable: the call returned the responses
        return {**base, "ok": True, "seconds": round(time.monotonic() - started, 1), "responses": to_jsonable(result, sink)}

    instance = result
    intermediate: list[Any] = []
    subscription = None
    try:
        subscription = instance.subscribe_to_intermediate_responses()
        subscription.add_callback(lambda item: intermediate.append(to_jsonable(item, sink)))
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
        "responses": to_jsonable(responses, sink),
        "intermediate_responses": intermediate,
    }
