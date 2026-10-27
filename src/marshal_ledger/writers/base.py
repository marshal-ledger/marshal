"""The writer interface shared by every format."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

import yaml

from ..model import AgentVersion


@dataclass
class Written:
    """A document produced by a writer, with notes explaining lost or filled-in fields.

    Note keys are field paths as reported by :func:`marshal_ledger.fidelity.field_values`,
    or glob patterns over them.
    """

    document: dict[str, Any]
    notes: dict[str, str] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)
    """For formats that span several files: relative path → file text. ``document`` is then the main file."""
    warnings: list[str] = field(default_factory=list)
    """Problems with the written document itself, such as required values the source lacks."""


class Writer(ABC):
    format: ClassVar[str]
    suffix: ClassVar[str]
    """File suffix for written documents, such as ``.adl.yaml``."""
    reader_format: ClassVar[str]
    """The reader used to read written documents back for the fidelity report."""
    LOSSES: ClassVar[dict[str, str]] = {}
    """Declared losses: fields this format can never express, keyed by path pattern.
    Round-trip tests fail on any loss that isn't declared here or noted by the writer."""
    MECHANICS: ClassVar[tuple[str, ...]] = ()
    """Path patterns for format mechanics (such as a format version) left out of fidelity reports."""

    @abstractmethod
    def write(self, record: AgentVersion) -> Written:
        """Map one ledger record to a document in this format."""

    def dump(self, document: dict[str, Any]) -> str:
        if self.suffix.endswith(".json"):
            return json.dumps(document, indent=2, ensure_ascii=False) + "\n"
        return yaml_text(document)


class _Dumper(yaml.SafeDumper):
    """Writes multi-line text, such as instructions, as a literal block that reads as written."""


_Dumper.add_representer(
    str, lambda dumper, value: dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|" if "\n" in value else None)
)


def yaml_text(data: Any, width: int = 100) -> str:
    """YAML for a written document: keys in order, Unicode as is, multi-line strings as blocks."""
    return yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=width)


def scope_value(value: Any) -> Any:
    return getattr(value, "value", value)
