"""Format writers. Each maps a ledger record out to one format."""

from __future__ import annotations

from .adl import AdlWriter
from .agentspec import AgentSpecWriter
from .base import Writer, Written
from .cloudformation import CloudFormationWriter
from .copilotstudio import CopilotStudioWriter
from .oasf import OasfWriter

WRITERS: dict[str, type[Writer]] = {
    AdlWriter.format: AdlWriter,
    AgentSpecWriter.format: AgentSpecWriter,
    OasfWriter.format: OasfWriter,
    CloudFormationWriter.format: CloudFormationWriter,
    CopilotStudioWriter.format: CopilotStudioWriter,
}

__all__ = ["WRITERS", "Writer", "Written"]
