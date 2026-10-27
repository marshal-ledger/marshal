"""The Terraform reader on real plans of public examples (tests/fixtures/terraform/README.md)."""

from pathlib import Path

import pytest

from marshal_ledger.readers import detect
from marshal_ledger.readers.terraform import TerraformReader

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "terraform"


def records(name):
    path = FIXTURES / f"{name}.plan.json"
    if not path.exists():
        pytest.skip("Terraform fixtures are not in this source archive")
    return {r.agent: r for r in TerraformReader().read(path).records}


def test_plans_are_detected_as_terraform():
    assert detect(FIXTURES / "acwwat-basic.plan.json") is TerraformReader


def test_aws_provider_agent_with_action_group_lambda_and_roles():
    [agent] = records("acwwat-basic").values()
    spec = agent.spec
    assert (agent.agent, agent.platform, spec.display_name.value) == ("aws_bedrockagent_agent.forex_asst", "bedrock", "ForexAssistant")
    assert spec.model.requested.value == "anthropic.claude-3-haiku-20240307-v1:0"
    assert [t.name for t in spec.tools] == ["GET /currencies", "GET /currencies/{code}"]
    assert spec.permissions.principal.value == "AmazonBedrockExecutionRoleForAgents_ForexAssistant"
    assert [(g.privilege, g.scope.value) for g in spec.permissions.grants] == [("bedrock:InvokeModel", "execute")]
    assert set(agent.bindings["executors"].values()) == {"function:ForexAPI"}


def test_knowledge_base_association():
    [agent] = records("acwwat-knowledge-base").values()
    [kb] = agent.spec.knowledge
    assert kb.ref.startswith("knowledge/bedrock.knowledge_base.aws_bedrockagent_knowledge_base.")


def test_cloud_control_agents_in_modules():
    found = records("aws-ia-agent-only")
    [agent] = found.values()
    assert agent.agent == "module.bedrock.awscc_bedrock_agent.bedrock_agent[0]"
    assert agent.spec.model.requested.value == "anthropic.claude-v2"
    assert agent.spec.instructions.text.startswith("You are an automotive assisant")
    # The module prefixes the name with a random string, known only after apply.
    assert not agent.spec.display_name.is_known and "random_string.solution_prefix" in agent.spec.display_name.basis
    # The role comes from a module resource, through a conditional on a module input.
    assert agent.spec.permissions.identity.value.value == "service_principal"
    assert agent.spec.extensions["bedrock"]["aliases"]


def test_guardrail_known_only_after_apply_is_still_recorded():
    [agent] = records("aws-ia-agent-with-guardrails").values()
    assert set(agent.spec.extensions["bedrock"]["guardrail"]) == {"GuardrailIdentifier", "GuardrailVersion"}


def test_supervisor_and_collaborators_across_modules():
    found = records("aws-ia-agent-collaborator")
    supervisor = found["module.agent_supervisor.aws_bedrockagent_agent.agent_supervisor[0]"]
    assert supervisor.spec.extensions["bedrock"]["collaboration"] == "SUPERVISOR"
    # Each collaborator module names the supervisor through a module input fed by the supervisor
    # module's output. Its alias comes through a local, which a plan doesn't record.
    assert len(supervisor.spec.extensions["bedrock"]["external_collaborators"]) == 2
    assert len(found) == 3
