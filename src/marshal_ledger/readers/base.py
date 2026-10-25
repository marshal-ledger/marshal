"""The reader interface shared by every format."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Optional

from ..hashing import spec_digest
from ..model import AgentSpec, AgentVersion, Entity, Source
from ..quality import score


@dataclass
class Skipped:
    """An item in the artifact that the reader looked at and did not record as an agent."""

    artifact: str
    locator: str
    reason: str


@dataclass
class ReadResult:
    records: list[AgentVersion] = field(default_factory=list)
    entities: list[Entity] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ReadOptions:
    target: Optional[str] = None
    """Apply this deployment target's overrides, where the format has targets."""
    hash_only: bool = False
    """Store instruction hashes but not instruction text."""
    commit: Optional[str] = None
    pipeline_run: Optional[str] = None


class Reader(ABC):
    format: ClassVar[str]
    platform: ClassVar[str]

    @classmethod
    @abstractmethod
    def detect(cls, path: Path) -> bool:
        """Return True if ``path`` looks like an artifact of this format."""

    @abstractmethod
    def read(self, path: Path, options: ReadOptions | None = None) -> ReadResult:
        """Read every agent in the artifact at ``path``."""


def build_record(
    *,
    agent: str,
    platform: str,
    source: Source,
    spec: AgentSpec,
    bindings: dict[str, Any] | None = None,
) -> AgentVersion:
    """Assemble an :class:`AgentVersion`, computing its version hash and quality."""
    return AgentVersion(
        agent=agent,
        platform=platform,
        version=spec_digest(spec),
        source=source,
        spec=spec,
        bindings=bindings or {},
        quality=score(spec),
    )
