"""Conversion between formats, through the ledger model."""

from __future__ import annotations

from dataclasses import dataclass
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


class ConversionError(ValueError):
    pass


def convert(record: AgentVersion, target: str) -> Conversion:
    """Write ``record`` in the ``target`` format and measure what survived."""
    writer_cls = WRITERS.get(target)
    if writer_cls is None:
        raise ConversionError(f"no writer for {target!r}; writers: {', '.join(sorted(WRITERS))}")
    writer = writer_cls()
    written = writer.write(record)
    reader = READERS[writer.reader_format]()
    assert isinstance(reader, DocumentReader)
    filename = record.agent.rsplit("/", 1)[-1].split(":")[-1] or "agent"
    filename = "".join(c if c.isalnum() or c in "-_." else "-" for c in filename) + writer.suffix
    result = reader.read_data(written.document, filename)
    if len(result.records) != 1:
        raise ConversionError(f"{target} writer produced {len(result.records)} agents for one record")
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
    )
