"""Format readers. Each maps one source format into the ledger model."""

from __future__ import annotations

from pathlib import Path

from .base import ReadOptions, ReadResult, Reader, Skipped
from .databricks import DatabricksBundleReader

READERS: dict[str, type[Reader]] = {
    DatabricksBundleReader.format: DatabricksBundleReader,
}


def detect(path: Path) -> type[Reader] | None:
    """Return the reader for the artifact at ``path``, or None if no reader recognizes it."""
    for reader in READERS.values():
        if reader.detect(path):
            return reader
    return None


__all__ = ["READERS", "ReadOptions", "ReadResult", "Reader", "Skipped", "detect"]
