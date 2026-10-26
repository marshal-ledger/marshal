"""Golden tests for the CloudFormation reader against public AWS sample templates."""

import os
from pathlib import Path

import pytest

from marshal_ledger.model import IdentityKind, Provenance, Scope
from marshal_ledger.readers.cloudformation import CloudFormationReader

CACHE = Path(os.environ.get("MARSHAL_FIXTURE_CACHE") or Path(__file__).resolve().parent.parent / "fixtures" / ".cache")


def records(fixture_id):
    path = CACHE / fixture_id
    if not path.is_dir():
        pytest.skip(f"fixture {fixture_id} not fetched; run python scripts/fetch_fixtures.py")
    return {r.agent: r for r in CloudFormationReader().read(path).records}


def test_parameter_defaults_and_executor_role_scopes():
    agent = records("cloudformation/bedrock-agents")["cost-explorer-agent/agent_template/BillingAgent"]
    spec = agent.spec
    # Instruction and model come from parameter defaults: known, but a deploy can override them.
    assert spec.instructions.provenance is Provenance.DERIVED
    assert "default of template parameter AgentInstruction" in spec.instructions.basis
    assert spec.model.provider.value == "anthropic"
    # Each action group's scope comes from its executor Lambda's IAM role (ce:Get*, ce:List*).
    tools = {t.name: t for t in spec.tools}
    assert tools["SavingsPlanAgent"].scope.value is Scope.READ
    assert "SavingsPlanAgentLambdaRole" in tools["SavingsPlanAgent"].scope.basis
    assert tools["UserInputAction"].ref == "tool/bedrock.user_input"
    # The agent's own role: identity and grants.
    assert spec.permissions.identity.value is IdentityKind.SERVICE_PRINCIPAL
    assert {g.privilege for g in spec.permissions.grants} == {"s3:GetObject", "bedrock:InvokeModel"}
    assert spec.extensions["bedrock"]["aliases"] == ["beta"]


def test_inline_openapi_action_groups():
    agent = records("cloudformation/bedrock-agents")["fine-grained-access-permissions-agent/003_bedrock-agent/template/BedrockAgent"]
    scopes = {t.name: t.scope.value for t in agent.spec.tools}
    assert scopes["GET /listClaims"] is Scope.READ
    assert scopes["PUT /updateClaim/{claimId}"] is Scope.WRITE


def test_conditions_are_evaluated_with_parameter_defaults():
    agents = records("cloudformation/serverless-agents")
    assert agents["api-inspector-agent/ApiInspectorAgent"].spec.knowledge == []
    assert "is false with default parameters" in agents["api-inspector-agent/ApiInspectorAgent"].spec.extensions["bedrock"]["conditional"]["KnowledgeBases"]
    [kb] = agents["api-expert-agent/ApiExpertAgent"].spec.knowledge
    assert kb.ref == "knowledge/bedrock.knowledge_base.${import:your-kb-stack-name-KnowledgeBaseId}"


def test_supervisor_with_external_collaborators():
    [agent] = records("cloudformation/multi-agent").values()
    assert agent.spec.extensions["bedrock"]["collaboration"] == "SUPERVISOR"
    assert len(agent.spec.extensions["bedrock"]["external_collaborators"]) == 3


def test_agentcore_runtimes_are_inventory():
    agents = records("cloudformation/agentcore")
    runtime = agents["basic-runtime/template/AgentRuntime"]
    assert runtime.platform == "bedrock-agentcore"
    assert runtime.spec.model.requested.provenance is Provenance.UNKNOWN
    assert runtime.spec.permissions.identity.value is IdentityKind.SERVICE_PRINCIPAL
    assert runtime.spec.permissions.grants
    assert len([a for a in agents if a.startswith("multi-agent-runtime/")]) == 2
