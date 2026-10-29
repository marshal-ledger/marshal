"""Ingestion: the one path by which records enter a store.

Both ``marshal collect`` (local mode) and the ingest API call :func:`ingest`.
The store never trusts a client's version hash or quality score: records are
re-normalized here, so a store holds the same result whichever client sent it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import datetime
from typing import Any, Iterable, Optional

from .diff import diff_specs
from .hashing import sha256_text, spec_digest
from .model import AgentSpec, AgentVersion, Channel, DeploymentEvent, Entity, Provenance
from .quality import score
from .redact import redact
from .risk import RankedChange, RiskRules, rank
from .store import Actor, Store, StoreError


@dataclass
class AgentReport:
    """What one ingested record changed, ranked by risk."""

    agent: str
    platform: str
    event: str
    environment: str
    version: str
    label: str
    new_version: bool
    baseline: Optional[str]
    """Label of the version last shipped to the environment before this event, if any."""
    completeness: float
    changes: list[RankedChange] = field(default_factory=list)
    redactions: int = 0
    channel: str = Channel.MANUAL.value
    portability: dict[str, int] = field(default_factory=dict)
    """Target platform display name → portability score, when requested."""
    drift: bool = False
    """An observed live definition differs from what was last shipped."""

    def to_dict(self) -> dict[str, Any]:
        data = {k: v for k, v in self.__dict__.items() if k != "changes"}
        data["changes"] = [c.to_dict() for c in self.changes]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentReport":
        changes = [RankedChange.from_dict(c) for c in data.get("changes", [])]
        # Unknown keys are skipped, so a client can read a newer server's reports.
        known = {f.name for f in fields(cls)} - {"changes"}
        return cls(**{k: v for k, v in data.items() if k in known}, changes=changes)


def normalize(record: AgentVersion, *, hash_only: bool = False) -> tuple[AgentVersion, int]:
    """Redact free text, apply hash-only mode, and recompute the version hash and quality."""
    record = record.model_copy(deep=True)
    spec = record.spec
    redactions = 0
    if spec.instructions.text is not None:
        text, count = redact(spec.instructions.text)
        redactions += count
        spec.instructions.text = text
        spec.instructions.hash = sha256_text(text)
        if spec.instructions.provenance is Provenance.UNKNOWN:
            spec.instructions.provenance = Provenance.DECLARED
    if hash_only:
        spec.instructions.text = None
    for value in (spec.description, spec.display_name):
        if isinstance(value.value, str):
            value.value, count = redact(value.value)
            redactions += count
    record.version = spec_digest(spec)
    record.quality = score(spec)
    return record, redactions


def ingest(
    store: Store,
    records: Iterable[AgentVersion],
    *,
    event: DeploymentEvent,
    environment: str,
    entities: Iterable[Entity] = (),
    actor: Actor | None = None,
    hash_only: bool = False,
    at: datetime | None = None,
    channel: Channel = Channel.MANUAL,
    risk: RiskRules | None = None,
) -> list[AgentReport]:
    """Normalize and record ``records``, and report each one's changes against what
    was last shipped to ``environment``, rated by the built-in rules and ``risk``."""
    normalized = [normalize(r, hash_only=hash_only) for r in records]

    baselines: dict[str, tuple[Optional[str], Optional[AgentVersion]]] = {}
    for record, _ in normalized:
        try:
            shipped = store.resolve(record.agent, f"shipped:{environment}")
            baselines[record.agent] = (shipped.label, shipped.record)
        except StoreError:
            baselines[record.agent] = (None, None)

    recorded = store.record(
        [r for r, _ in normalized], event=event, environment=environment, entities=entities, at=at, actor=actor,
        channel=channel,
    )

    reports = []
    for (record, redactions), result in zip(normalized, recorded):
        baseline_label, baseline = baselines[record.agent]
        baseline_spec = baseline.spec if baseline is not None else AgentSpec()
        reports.append(
            AgentReport(
                agent=record.agent,
                platform=record.platform,
                event=event.value,
                environment=environment,
                version=record.version,
                label=result.version.label,
                new_version=result.new_version,
                baseline=baseline_label,
                completeness=record.quality.completeness,
                changes=rank(diff_specs(baseline_spec, record.spec), risk, before=baseline, after=record),
                redactions=redactions,
                channel=channel.value,
                drift=event is DeploymentEvent.OBSERVED and baseline_label is not None and baseline_label != result.version.label,
            )
        )
    return reports
