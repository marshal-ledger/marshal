"""Risk ranking for agent changes.

Each change from :mod:`marshal_ledger.diff` gets a severity. The rules put the
changes a reviewer most needs to see first: escalated permissions, new tools
that can write, a new model provider, instructions that lost a guardrail, and
removed knowledge. An unknown scope is ranked as if it were a write, because the
ledger does not assume a tool is safe.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Optional

from .diff import Change, describe

_SCOPE_RANK = {"read": 1, "execute": 2, "write": 3, "unknown": 3, "admin": 4}


class Severity(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3

    @property
    def label(self) -> str:
        return {Severity.LOW: "LOW", Severity.MEDIUM: "MED", Severity.HIGH: "HIGH"}[self]


@dataclass(frozen=True)
class RankedChange:
    severity: Severity
    change: Change

    def to_dict(self) -> dict[str, Any]:
        return {"severity": self.severity.label, **self.change.to_dict()}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RankedChange":
        data = dict(data)
        severity = {s.label: s for s in Severity}[data.pop("severity")]
        return cls(severity, Change(**data))

    def line(self) -> str:
        return f"{self.severity.label:<4}  {describe(self.change)}"


def _rank(scope: Optional[str]) -> int:
    return _SCOPE_RANK.get(str(scope), _SCOPE_RANK["unknown"])


# Platform-specific fields that control who the agent acts as or who may use it.
SENSITIVE_EXTENSION_KEYS: dict[str, Severity] = {
    "connection_mode": Severity.HIGH,
    "credentials": Severity.HIGH,
    "authenticationMode": Severity.HIGH,
    "accessControlPolicy": Severity.HIGH,
    "default_agent_user": Severity.HIGH,
    "user_api_scopes": Severity.HIGH,
    "authorizer": Severity.HIGH,
    "managed_policies": Severity.HIGH,
    "guardrail": Severity.MEDIUM,
    "security": Severity.MEDIUM,
    "permissions": Severity.MEDIUM,
}


def severity(change: Change) -> Severity:
    path, kind = change.path, change.kind

    if change.category == "extensions":
        parts = set(path.split("."))
        found = [level for key, level in SENSITIVE_EXTENSION_KEYS.items() if key in parts]
        return max(found) if found else Severity.LOW

    if change.category == "permissions":
        if path == "permissions.identity":
            return Severity.HIGH
        if path == "permissions.principal":
            return Severity.MEDIUM
        if path.startswith("grant "):
            if kind == "added":
                return Severity.HIGH if _rank(change.scope) >= _rank("write") else Severity.MEDIUM
            return Severity.LOW
        if path.startswith("access "):
            return Severity.MEDIUM if kind == "added" else Severity.LOW

    if change.category == "tools":
        if kind == "added":
            if _rank(change.scope) >= _rank("write") or change.reversible == "false":
                return Severity.HIGH
            return Severity.MEDIUM if _rank(change.scope) == _rank("execute") else Severity.LOW
        if kind == "removed":
            return Severity.LOW
        if path.endswith(" scope"):
            return Severity.HIGH if _rank(change.after) > _rank(change.before) else Severity.LOW
        if path.endswith(" reversible"):
            after = str(change.after).lower()
            if after == "false":
                return Severity.HIGH
            return Severity.MEDIUM if after == "unknown" else Severity.LOW
        if path.endswith(" requires_confirmation"):
            # Losing a human approval step is an escalation.
            return Severity.HIGH if str(change.before).lower() == "true" else Severity.LOW
        return Severity.LOW

    if change.category == "model":
        # A routine upgrade within one provider is reviewed, not escalated; a new provider
        # means a new party sees the agent's data.
        return Severity.HIGH if path == "model.provider" else Severity.MEDIUM

    if change.category == "instructions":
        # Instructions often carry the agent's guardrails, so any change is reviewed.
        return Severity.HIGH if change.guardrails else Severity.MEDIUM

    if change.category == "knowledge":
        if kind == "removed":
            return Severity.MEDIUM
        if kind == "added":
            if change.scope == "unknown":
                return Severity.MEDIUM
            return Severity.HIGH if _rank(change.scope) >= _rank("write") else Severity.LOW
        if _rank(change.after) > _rank(change.before):
            return Severity.HIGH if change.after == "write" else Severity.MEDIUM
        return Severity.LOW

    return Severity.LOW


def rank(changes: list[Change]) -> list[RankedChange]:
    """Changes sorted by severity, highest first. Ties keep their diff order."""
    ranked = [RankedChange(severity(c), c) for c in changes]
    return sorted(ranked, key=lambda r: -r.severity)
