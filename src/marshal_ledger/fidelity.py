"""Translation fidelity, computed by reading the output back.

A conversion writes the target document, reads it back with the target
format's reader, and compares the two records field by field:

- **lossless**: the field reads back with the same value.
- **approximated**: it reads back with a different value.
- **dropped**: it does not read back at all.
- **defaulted**: it reads back although the source had no value, because the
  target format requires one and the writer had to fill it in.

Because the report comes from the read-back record, it can't claim a field
survived when it didn't. Writers add notes explaining why a field was lost.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterator

from .hashing import canonical_json
from .model import AgentVersion, Value


class Outcome(str, Enum):
    LOSSLESS = "lossless"
    APPROXIMATED = "approximated"
    DROPPED = "dropped"
    DEFAULTED = "defaulted"


@dataclass(frozen=True)
class FieldOutcome:
    path: str
    outcome: Outcome
    before: Any = None
    after: Any = None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {"path": self.path, "outcome": self.outcome.value}
        for key in ("before", "after", "note"):
            if getattr(self, key) is not None:
                data[key] = getattr(self, key)
        return data


@dataclass
class FidelityReport:
    target: str
    agent: str
    fields: list[FieldOutcome] = field(default_factory=list)

    def count(self, outcome: Outcome) -> int:
        return sum(1 for f in self.fields if f.outcome is outcome)

    @property
    def lossy(self) -> list[FieldOutcome]:
        return [f for f in self.fields if f.outcome is not Outcome.LOSSLESS]

    def summary(self) -> str:
        parts = [f"{self.count(Outcome.LOSSLESS)} lossless"]
        for outcome in (Outcome.APPROXIMATED, Outcome.DROPPED, Outcome.DEFAULTED):
            if n := self.count(outcome):
                parts.append(f"{n} {outcome.value}")
        return " · ".join(parts)

    def text(self) -> str:
        marks = {Outcome.APPROXIMATED: "~", Outcome.DROPPED: "-", Outcome.DEFAULTED: "+"}
        lines = [f"{self.agent} → {self.target}", f"  Fidelity: {self.summary()}"]
        # A platform's dropped extension fields share one reason; show them as one line.
        groups: dict[tuple[str, str | None], list[str]] = {}
        items = []
        for item in self.lossy:
            parts = item.path.split(".")
            if item.outcome is Outcome.DROPPED and parts[0] == "extensions" and len(parts) > 2:
                key = (f"extensions.{parts[1]}", item.note)
                if key not in groups:
                    items.append(key)
                groups.setdefault(key, []).append(item.path)
            else:
                items.append(item)
        for item in items:
            if isinstance(item, tuple):
                prefix, note = item
                count = len(groups[item])
                line = f"    - {prefix}.* ({count} field{'s' * (count != 1)}" + (f"; {note})" if note else ")")
                lines.append(line)
                continue
            line = f"    {marks[item.outcome]} {item.path}"
            if item.outcome is Outcome.APPROXIMATED:
                line += f": {_short(item.before)} → {_short(item.after)}"
            if item.note:
                line += f" ({item.note})"
            lines.append(line)
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "agent": self.agent,
            "summary": {o.value: self.count(o) for o in Outcome},
            "fields": [f.to_dict() for f in self.fields],
        }


def _short(value: Any, limit: int = 60) -> str:
    text = value if isinstance(value, str) else canonical_json(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _known(v: Value) -> Any:
    if not v.is_known:
        return None
    return getattr(v.value, "value", v.value)


def field_values(record: AgentVersion) -> dict[str, Any]:
    """Every populated field of a record, keyed by a stable path, with its comparable value."""
    spec = record.spec
    values: dict[str, Any] = {"agent": record.agent, "platform": record.platform}

    def put(path: str, value: Any) -> None:
        if value is not None and value != {} and value != []:
            values[path] = value

    put("display_name", _known(spec.display_name))
    put("description", _known(spec.description))
    put("model.requested", _known(spec.model.requested))
    put("model.provider", _known(spec.model.provider))
    put("model.resolved", _known(spec.model.resolved))
    for key, value in spec.model.parameters.items():
        put(f"model.parameters.{key}", value)
    put("instructions", spec.instructions.hash)
    for tool in spec.tools:
        base = f"tools[{tool.ref}]"
        values[base] = True
        for attr in ("name", "description", "input_schema", "output_schema"):
            put(f"{base}.{attr}", getattr(tool, attr))
        for attr in ("scope", "reversible", "requires_confirmation"):
            put(f"{base}.{attr}", _known(getattr(tool, attr)))
    for item in spec.knowledge:
        base = f"knowledge[{item.ref}]"
        values[base] = True
        for attr in ("name", "description", "type"):
            put(f"{base}.{attr}", getattr(item, attr))
        put(f"{base}.scope", _known(item.scope))
    put("permissions.identity", _known(spec.permissions.identity))
    put("permissions.principal", _known(spec.permissions.principal))
    for grant in spec.permissions.grants:
        values[f"permissions.grants[{grant.target} {grant.privilege}]"] = grant.scope.value
    for rule in spec.permissions.access:
        values[f"permissions.access[{rule.principal} {rule.level}]"] = rule.principal_type
    for platform, block in spec.extensions.items():
        for key, value in block.items():
            put(f"extensions.{platform}.{key}", value)
    return values


def matches(path: str, pattern: str) -> bool:
    """Match a field path against a pattern in which only ``*`` is a wildcard."""
    regex = "".join(".*" if part == "*" else re.escape(part) for part in re.split(r"(\*)", pattern))
    return re.fullmatch(regex, path) is not None


def _note(path: str, notes: dict[str, str]) -> str | None:
    if path in notes:
        return notes[path]
    for pattern, note in notes.items():
        if matches(path, pattern):
            return note
    return None


def compare(
    source: AgentVersion,
    written: AgentVersion,
    target: str,
    notes: dict[str, str],
    ignore: tuple[str, ...] = (),
) -> FidelityReport:
    """Field-by-field fidelity of ``written`` (the read-back record) against ``source``.

    ``ignore`` lists path patterns for format mechanics, such as a format version
    field, that say nothing about the agent.
    """
    before, after = field_values(source), field_values(written)
    for values in (before, after):
        for path in [p for p in values if any(matches(p, i) for i in ignore)]:
            del values[path]
    report = FidelityReport(target=target, agent=source.agent)
    dropped_items = {p for p in before if p.endswith("]") and p not in after}
    for path, value in before.items():
        parent = path.rsplit("].", 1)[0] + "]" if "]." in path else None
        if parent in dropped_items:
            continue  # reported once, with the item
        if path not in after:
            report.fields.append(FieldOutcome(path, Outcome.DROPPED, before=value, note=_note(path, notes)))
        elif canonical_json(after[path]) != canonical_json(value):
            report.fields.append(
                FieldOutcome(path, Outcome.APPROXIMATED, before=value, after=after[path], note=_note(path, notes))
            )
        else:
            report.fields.append(FieldOutcome(path, Outcome.LOSSLESS))
    # In read-back order, so a report is the same on every run (a set difference is not).
    for path in (p for p in after if p not in before):
        report.fields.append(FieldOutcome(path, Outcome.DEFAULTED, after=after[path], note=_note(path, notes)))
    return report


def iter_paths(report: FidelityReport, outcome: Outcome) -> Iterator[str]:
    return (f.path for f in report.fields if f.outcome is outcome)
