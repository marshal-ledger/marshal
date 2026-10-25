"""Completeness scoring.

Completeness is the weighted share of a record's fields whose value is known.
Fields that matter most for governance weigh most: who the agent acts as, and
what its tools can change.
"""

from __future__ import annotations

from typing import Iterator

from .model import AgentSpec, Provenance, Quality, Value

# Weight per field path. List items use ``[]`` in place of an index.
WEIGHTS: dict[str, float] = {
    "permissions.identity": 3.0,
    "permissions.principal": 2.0,
    "tools[].scope": 3.0,
    "tools[].reversible": 2.0,
    "knowledge[].scope": 1.0,
    "model.requested": 2.0,
    "instructions": 2.0,
    "description": 0.5,
    "display_name": 0.5,
}


def _fields(spec: AgentSpec) -> Iterator[tuple[str, str, bool]]:
    """Yield (weight key, concrete path, is_known) for every scored field."""

    def value(key: str, path: str, v: Value) -> tuple[str, str, bool]:
        return key, path, v.is_known

    yield value("display_name", "display_name", spec.display_name)
    yield value("description", "description", spec.description)
    yield value("model.requested", "model.requested", spec.model.requested)
    yield (
        "instructions",
        "instructions",
        spec.instructions.provenance is not Provenance.UNKNOWN,
    )
    yield value("permissions.identity", "permissions.identity", spec.permissions.identity)
    yield value("permissions.principal", "permissions.principal", spec.permissions.principal)
    for i, tool in enumerate(spec.tools):
        yield value("tools[].scope", f"tools[{i}].scope", tool.scope)
        yield value("tools[].reversible", f"tools[{i}].reversible", tool.reversible)
    for i, item in enumerate(spec.knowledge):
        yield value("knowledge[].scope", f"knowledge[{i}].scope", item.scope)


def score(spec: AgentSpec) -> Quality:
    total = known = 0.0
    unknown: list[str] = []
    for key, path, is_known in _fields(spec):
        weight = WEIGHTS[key]
        total += weight
        if is_known:
            known += weight
        else:
            unknown.append(path)
    completeness = round(known / total, 2) if total else 0.0
    return Quality(completeness=completeness, unknown_fields=unknown)
