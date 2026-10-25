"""Format writers. Each maps a ledger record out to one format."""

from __future__ import annotations

from .adkconfig import AdkConfigWriter
from .adl import AdlWriter
from .agentforce import AgentforceWriter
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
    AgentforceWriter.format: AgentforceWriter,
    AdkConfigWriter.format: AdkConfigWriter,
}

__all__ = ["WRITERS", "Writer", "Written"]
