"""Confirmation policy for SiLA 2 calls: which calls need the user's approval, approvals
remembered for a while, and an audit log.

Kept free of MCP; server.py asks the user (MCP elicitation) when `needs_approval` says so.
"""

from __future__ import annotations

import datetime
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

Mode = Literal["none", "every", "once"]
MODES: tuple[str, ...] = ("none", "every", "once")
Kind = Literal["command", "property"]


def check_mode(value: Any, where: str) -> Mode:
    if value not in MODES:
        raise ValueError(f"{where} must be one of {', '.join(MODES)}, not {value!r}")
    return value


def parse_ttl_minutes(value: Any, where: str = "approval_ttl_minutes") -> float | None:
    """Minutes as a positive number, or "unlimited" -> None (approvals never expire)."""
    if isinstance(value, str) and value.strip().lower() == "unlimited":
        return None
    try:
        minutes = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'{where} must be a number of minutes or "unlimited", not {value!r}') from None
    if isinstance(value, bool) or not minutes > 0:
        raise ValueError(f'{where} must be a positive number of minutes or "unlimited", not {value!r}')
    return minutes


@dataclass(frozen=True)
class Policy:
    """Confirmation modes: `none` (never ask), `every` (ask each time), `once` (ask once per
    command or property, then remember the approval for `ttl_minutes`; None = unlimited)."""

    commands: Mode = "once"
    properties: Mode = "none"
    ttl_minutes: float | None = 30.0

    def mode(self, kind: Kind) -> Mode:
        return self.commands if kind == "command" else self.properties

    def describe(self) -> dict[str, Any]:
        return {
            "commands": self.commands,
            "properties": self.properties,
            "approval_ttl_minutes": "unlimited" if self.ttl_minutes is None else self.ttl_minutes,
        }


Key = tuple[str, str, str, str]  # (kind, server, feature, command or property)


class Approvals:
    """Approvals given in `once` mode, kept in memory for this process (one MCP session)."""

    def __init__(self) -> None:
        self._granted: dict[Key, float] = {}  # key -> time.monotonic() of the approval
        self._lock = threading.Lock()

    def is_granted(self, key: Key, ttl_minutes: float | None) -> bool:
        with self._lock:
            granted = self._granted.get(key)
            if granted is None:
                return False
            if ttl_minutes is not None and time.monotonic() - granted > ttl_minutes * 60:
                del self._granted[key]
                return False
            return True

    def grant(self, key: Key) -> None:
        with self._lock:
            self._granted[key] = time.monotonic()

    def revoke(self, server: str | None = None) -> int:
        """Forget approvals (of one server, or all); returns how many were forgotten."""
        with self._lock:
            keys = [k for k in self._granted if server is None or k[1] == server]
            for k in keys:
                del self._granted[k]
            return len(keys)


def needs_approval(policy: Policy, approvals: Approvals, key: Key) -> bool:
    mode = policy.mode(key[0])  # type: ignore[arg-type]
    if mode == "none":
        return False
    if mode == "once" and approvals.is_granted(key, policy.ttl_minutes):
        return False
    return True


def default_audit_log() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return Path(base) / "sila2-mcp" / "audit.jsonl"


class AuditLog:
    """Appends one JSON object per line: approvals, refusals and executed calls."""

    def __init__(self, path: Path | None):
        self.path = path
        self._lock = threading.Lock()

    def write(self, event: str, **fields: Any) -> None:
        if self.path is None:
            return
        record = {"time": datetime.datetime.now().astimezone().isoformat(timespec="milliseconds"), "event": event}
        record.update(fields)
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
