"""Format readers. Each maps one source format into the ledger model."""

from __future__ import annotations

from pathlib import Path

from .adkconfig import AdkConfigReader
from .adl import AdlReader
from .agentcorecli import AgentCoreCliReader
from .agentengine import AgentEngineReader
from .agentforce import AgentforceReader
from .agentspec import AgentSpecReader
from .cloudformation import CloudFormationReader
from .copilotstudio import CopilotStudioReader
from .base import DocumentReader, ReadOptions, ReadResult, Reader, Skipped
from .databricks import DatabricksBundleReader
from .marshalfile import MarshalFileReader
from .oasf import OasfReader
from .python import PythonAgentReader
from .salesforcemetadata import SalesforceMetadataReader
from .terraform import TerraformReader

# Detection order: bundle formats (identified by a file name) before document formats.
READERS: dict[str, type[Reader]] = {
    DatabricksBundleReader.format: DatabricksBundleReader,
    AdlReader.format: AdlReader,
    AgentSpecReader.format: AgentSpecReader,
    OasfReader.format: OasfReader,
    CloudFormationReader.format: CloudFormationReader,
    TerraformReader.format: TerraformReader,
    CopilotStudioReader.format: CopilotStudioReader,
    AgentforceReader.format: AgentforceReader,
    AdkConfigReader.format: AdkConfigReader,
    AgentEngineReader.format: AgentEngineReader,
    SalesforceMetadataReader.format: SalesforceMetadataReader,
    MarshalFileReader.format: MarshalFileReader,
    AgentCoreCliReader.format: AgentCoreCliReader,
    # Last: a directory of Python code is the least specific match.
    PythonAgentReader.format: PythonAgentReader,
}


def detect(path: Path) -> type[Reader] | None:
    """Return the reader for the artifact at ``path``, or None if no reader recognizes it."""
    for reader in READERS.values():
        if reader.detect(path):
            return reader
    return None


__all__ = ["READERS", "DocumentReader", "ReadOptions", "ReadResult", "Reader", "Skipped", "detect"]
