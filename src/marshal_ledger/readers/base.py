"""The reader interface shared by every format."""

from __future__ import annotations

import datetime
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Iterator, Optional

import yaml

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
    link_code: bool = True
    """Read agent code that a platform artifact points to, such as a Databricks App's source."""
    infer_code_agents: bool = True
    """Record model API calls that pass tools as agents, for code that uses no agent framework."""


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


_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".cache"}
_DOCUMENT_SUFFIXES = {".yaml", ".yml", ".json"}
MAX_DOCUMENT_BYTES = 5_000_000


def jsonable(node: Any) -> Any:
    """Convert YAML-only scalars (dates, timestamps) to their ISO 8601 strings."""
    if isinstance(node, dict):
        return {str(k): jsonable(v) for k, v in node.items()}
    if isinstance(node, list):
        return [jsonable(v) for v in node]
    if isinstance(node, (datetime.date, datetime.datetime)):
        return node.isoformat().replace("+00:00", "Z")
    return node


def load_document(path: Path) -> Any:
    """Parse a YAML or JSON file. Returns None for files that are not valid documents."""
    if path.stat().st_size > MAX_DOCUMENT_BYTES:
        return None
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".json":
            return json.loads(text)
        return jsonable(yaml.safe_load(text))
    except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError):
        return None


def iter_document_files(path: Path) -> Iterator[Path]:
    """The YAML and JSON files at ``path``: the file itself, or every file under a directory."""
    if path.is_file():
        yield path
        return
    for child in sorted(path.rglob("*")):
        if any(part in _SKIP_DIRS for part in child.relative_to(path).parts):
            continue
        if child.is_file() and child.suffix in _DOCUMENT_SUFFIXES:
            yield child


class DocumentReader(Reader):
    """A reader for formats where each agent is a standalone YAML or JSON document."""

    @classmethod
    @abstractmethod
    def matches(cls, data: Any) -> bool:
        """Return True if a parsed document is in this format."""

    @abstractmethod
    def read_data(self, data: Any, artifact: str, options: ReadOptions | None = None) -> ReadResult:
        """Read one parsed document. ``artifact`` names it in the records' source."""

    @classmethod
    def detect(cls, path: Path) -> bool:
        if not path.exists():
            return False
        return any(cls.matches(load_document(f)) for f in iter_document_files(path))

    def read(self, path: Path, options: ReadOptions | None = None) -> ReadResult:
        result = ReadResult()
        root = path if path.is_dir() else path.parent
        for file in iter_document_files(path):
            data = load_document(file)
            if not self.matches(data):
                continue
            part = self.read_data(data, file.relative_to(root).as_posix(), options)
            result.records += part.records
            result.skipped += part.skipped
            result.warnings += part.warnings
            known = {e.ref for e in result.entities}
            result.entities += [e for e in part.entities if e.ref not in known]
        return result


def slug(text: str) -> str:
    """A lowercase, hyphenated identifier for names that become agent ids or refs."""
    out = "".join(c.lower() if c.isalnum() else "-" for c in text.strip())
    return "-".join(filter(None, out.split("-"))) or "agent"


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
