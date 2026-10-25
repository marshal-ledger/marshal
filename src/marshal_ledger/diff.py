"""Normalized diff between two versions of an agent.

Changes are computed on the ledger model, not on source text, so the same
change reads the same way on every platform. Tools, knowledge and grants are
matched by entity reference. Provenance and basis metadata are ignored, except
that a value moving from unknown to known (or back) is reported.
"""

from __future__ import annotations

import difflib
from dataclasses import asdict, dataclass
from typing import Any, Iterator, Optional

from .model import AgentSpec, Instructions, Value

CATEGORIES = ("permissions", "tools", "model", "knowledge", "instructions", "metadata", "extensions")


@dataclass(frozen=True)
class Change:
    category: str
    """One of :data:`CATEGORIES`."""
    kind: str
    """``added``, ``removed`` or ``changed``."""
    path: str
    before: Any = None
    after: Any = None
    detail: Optional[str] = None
    scope: Optional[str] = None
    """For added or removed tools, knowledge and grants: the item's scope."""
    reversible: Optional[str] = None
    """For added or removed tools: whether the tool's effects are reversible."""

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


def _shown(v: Value) -> Any:
    if not v.is_known:
        return "unknown"
    value = v.value
    return getattr(value, "value", value)


def _value(category: str, path: str, a: Value, b: Value) -> Iterator[Change]:
    before, after = _shown(a), _shown(b)
    if before != after:
        yield Change(category, "changed", path, before, after)


def _instructions(a: Instructions, b: Instructions) -> Iterator[Change]:
    if a.hash == b.hash:
        return
    if a.hash is None or b.hash is None:
        yield Change(
            "instructions",
            "changed",
            "instructions",
            "unknown" if a.hash is None else a.hash,
            "unknown" if b.hash is None else b.hash,
        )
        return
    detail = "content changed"
    if a.text is not None and b.text is not None:
        added = removed = 0
        for line in difflib.ndiff(a.text.splitlines(), b.text.splitlines()):
            if line.startswith("+ "):
                added += 1
            elif line.startswith("- "):
                removed += 1
        detail = f"+{added} -{removed} lines"
    yield Change("instructions", "changed", "instructions", a.hash, b.hash, detail)


def _keyed(
    category: str,
    label: str,
    a: dict[str, Any],
    b: dict[str, Any],
    describe,
    compare,
    attrs=lambda item: {},
) -> Iterator[Change]:
    for key in sorted(b.keys() - a.keys()):
        yield Change(category, "added", f"{label} {key}", after=key, detail=describe(b[key]), **attrs(b[key]))
    for key in sorted(a.keys() - b.keys()):
        yield Change(category, "removed", f"{label} {key}", before=key, detail=describe(a[key]), **attrs(a[key]))
    for key in sorted(a.keys() & b.keys()):
        yield from compare(key, a[key], b[key])


def _flatten(node: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(node, dict) and node:
        flat: dict[str, Any] = {}
        for key, value in node.items():
            flat.update(_flatten(value, f"{prefix}.{key}" if prefix else str(key)))
        return flat
    return {prefix: node}


def diff_specs(a: AgentSpec, b: AgentSpec) -> list[Change]:
    """Every change from ``a`` to ``b``, grouped in :data:`CATEGORIES` order."""
    changes: list[Change] = []

    # permissions
    changes += _value("permissions", "permissions.identity", a.permissions.identity, b.permissions.identity)
    changes += _value("permissions", "permissions.principal", a.permissions.principal, b.permissions.principal)
    grants_a = {f"{g.target} {g.privilege}": g for g in a.permissions.grants}
    grants_b = {f"{g.target} {g.privilege}": g for g in b.permissions.grants}
    changes += _keyed(
        "permissions",
        "grant",
        grants_a,
        grants_b,
        lambda g: g.scope.value,
        lambda *_: iter(()),
        lambda g: {"scope": g.scope.value},
    )
    access_a = {f"{r.principal} {r.level}": r for r in a.permissions.access}
    access_b = {f"{r.principal} {r.level}": r for r in b.permissions.access}
    changes += _keyed("permissions", "access", access_a, access_b, lambda r: r.principal_type, lambda *_: iter(()))

    # tools
    def describe_tool(t) -> str:
        return f"scope {_shown(t.scope)}, reversible {_shown(t.reversible)}"

    def compare_tool(ref, x, y) -> Iterator[Change]:
        yield from _value("tools", f"tool {ref} scope", x.scope, y.scope)
        yield from _value("tools", f"tool {ref} reversible", x.reversible, y.reversible)
        yield from _value(
            "tools", f"tool {ref} requires_confirmation", x.requires_confirmation, y.requires_confirmation
        )
        for attr in ("name", "description", "input_schema", "output_schema"):
            before, after = getattr(x, attr), getattr(y, attr)
            if before != after:
                yield Change("tools", "changed", f"tool {ref} {attr}", before, after, f"{attr} changed")

    changes += _keyed(
        "tools",
        "tool",
        {t.ref: t for t in a.tools},
        {t.ref: t for t in b.tools},
        describe_tool,
        compare_tool,
        lambda t: {"scope": str(_shown(t.scope)), "reversible": str(_shown(t.reversible)).lower()},
    )

    # model
    changes += _value("model", "model.requested", a.model.requested, b.model.requested)
    changes += _value("model", "model.provider", a.model.provider, b.model.provider)
    changes += _value("model", "model.resolved", a.model.resolved, b.model.resolved)
    for key in sorted(a.model.parameters.keys() | b.model.parameters.keys()):
        before, after = a.model.parameters.get(key), b.model.parameters.get(key)
        if before != after:
            changes.append(Change("model", "changed", f"model.parameters.{key}", before, after))

    # knowledge
    def compare_knowledge(ref, x, y) -> Iterator[Change]:
        yield from _value("knowledge", f"knowledge {ref} scope", x.scope, y.scope)
        for attr in ("name", "description", "type"):
            before, after = getattr(x, attr), getattr(y, attr)
            if before != after:
                yield Change("knowledge", "changed", f"knowledge {ref} {attr}", before, after)

    changes += _keyed(
        "knowledge",
        "knowledge",
        {k.ref: k for k in a.knowledge},
        {k.ref: k for k in b.knowledge},
        lambda k: f"scope {_shown(k.scope)}",
        compare_knowledge,
        lambda k: {"scope": str(_shown(k.scope))},
    )

    changes += _instructions(a.instructions, b.instructions)

    changes += _value("metadata", "display_name", a.display_name, b.display_name)
    changes += _value("metadata", "description", a.description, b.description)

    ext_a, ext_b = _flatten(a.extensions), _flatten(b.extensions)
    ext_a.pop("", None)
    ext_b.pop("", None)
    for path in sorted(ext_b.keys() - ext_a.keys()):
        changes.append(Change("extensions", "added", f"extensions.{path}", after=ext_b[path]))
    for path in sorted(ext_a.keys() - ext_b.keys()):
        changes.append(Change("extensions", "removed", f"extensions.{path}", before=ext_a[path]))
    for path in sorted(ext_a.keys() & ext_b.keys()):
        if ext_a[path] != ext_b[path]:
            changes.append(Change("extensions", "changed", f"extensions.{path}", ext_a[path], ext_b[path]))

    return changes


_MARKS = {"added": "+", "removed": "-", "changed": "~"}


def describe(change: Change) -> str:
    """One line for a change, without severity: ``+ tool tool/x (scope write, ...)``."""
    mark = _MARKS[change.kind]
    if change.kind == "changed":
        text = change.detail or f"{change.before} → {change.after}"
        return f"{mark} {change.path}: {text}"
    return f"{mark} {change.path}" + (f" ({change.detail})" if change.detail else "")


def format_changes(header: str, changes: list[Change]) -> str:
    lines = [header]
    if not changes:
        lines.append("  no changes")
    lines += [f"  {describe(change)}" for change in changes]
    return "\n".join(lines)
