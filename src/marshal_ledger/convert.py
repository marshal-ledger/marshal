"""Conversion between formats, through the ledger model."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .fidelity import FidelityReport, compare
from .model import AgentVersion
from .readers import READERS, DocumentReader
from .writers import WRITERS


@dataclass
class Conversion:
    record: AgentVersion
    document: dict[str, Any]
    text: str
    filename: str
    report: FidelityReport
    read_back: AgentVersion
    warnings: list[str]
    files: dict[str, str] = field(default_factory=dict)
    """Set for formats that span several files: relative path → text."""


class ConversionError(ValueError):
    pass


def convert(record: AgentVersion, target: str, migrate: bool = False) -> Conversion:
    """Write ``record`` in the ``target`` format and measure what survived.

    By default the written agent keeps its platform, so reading it back gives the same agent.
    With ``migrate``, it moves to the target's platform: it keeps its id, so its history
    continues, and is recorded on the platform it will run on.
    """
    writer_cls = WRITERS.get(target)
    if writer_cls is None:
        raise ConversionError(f"no writer for {target!r}; writers: {', '.join(sorted(WRITERS))}")
    writer = writer_cls()
    if migrate:
        if writer.platform is None:
            raise ConversionError(f"{target} is an open format, not a platform to migrate to")
        record = record.model_copy(update={"platform": writer.platform})
    written = writer.write(record)
    reader = READERS[writer.reader_format]()
    filename = record.agent.rsplit("/", 1)[-1].split(":")[-1] or "agent"
    filename = "".join(c if c.isalnum() or c in "-_." else "-" for c in filename) + writer.suffix
    if written.files:
        with tempfile.TemporaryDirectory() as directory:
            for path, text in written.files.items():
                destination = Path(directory) / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(text, encoding="utf-8")
            result = reader.read(Path(directory))
    else:
        assert isinstance(reader, DocumentReader)
        result = reader.read_data(written.document, filename)
    if not result.records:
        raise ConversionError(f"{target} writer produced no agent that reads back")
    read_back = result.records[0]
    notes = {**writer.LOSSES, **written.notes}
    report = compare(record, read_back, target, notes, writer.MECHANICS)
    return Conversion(
        record=record,
        document=written.document,
        text=writer.dump(written.document),
        filename=filename,
        report=report,
        read_back=read_back,
        warnings=written.warnings,
        files=written.files,
    )
