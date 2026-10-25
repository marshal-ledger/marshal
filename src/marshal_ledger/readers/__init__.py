"""Format readers. Each maps one source format into the ledger model."""

from __future__ import annotations

from pathlib import Path

from .adl import AdlReader
from .agentspec import AgentSpecReader
from .base import DocumentReader, ReadOptions, ReadResult, Reader, Skipped
from .databricks import DatabricksBundleReader
from .oasf import OasfReader

# Detection order: bundle formats (identified by a file name) before document formats.
READERS: dict[str, type[Reader]] = {
    DatabricksBundleReader.format: DatabricksBundleReader,
    AdlReader.format: AdlReader,
    AgentSpecReader.format: AgentSpecReader,
    OasfReader.format: OasfReader,
}


def detect(path: Path) -> type[Reader] | None:
    """Return the reader for the artifact at ``path``, or None if no reader recognizes it."""
    for reader in READERS.values():
        if reader.detect(path):
            return reader
    return None


__all__ = ["READERS", "DocumentReader", "ReadOptions", "ReadResult", "Reader", "Skipped", "detect"]
