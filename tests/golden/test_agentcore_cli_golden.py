"""Golden tests for the AgentCore CLI reader against public AgentCore CLI projects."""

import os
from pathlib import Path

import pytest

from marshal_ledger.model import IdentityKind, Provenance, Scope
from marshal_ledger.readers import detect
from marshal_ledger.readers.agentcorecli import AgentCoreCliReader

CACHE = Path(os.environ.get("MARSHAL_FIXTURE_CACHE") or Path(__file__).resolve().parent.parent / "fixtures" / ".cache")


def path(fixture_id):
    found = CACHE / fixture_id
    if not found.is_dir():
        pytest.skip(f"fixture {fixture_id} not fetched; run python scripts/fetch_fixtures.py")
    return found


def records(fixture_id):
    result = AgentCoreCliReader().read(path(fixture_id))
    assert not result.skipped and not result.warnings
    return {r.agent: r for r in result.records}


def test_projects_are_detected():
    for fixture in ("agentcore-cli/the-pulse", "agentcore-cli/3d-rams", "agentcore-cli/ppt-agent"):
        assert detect(path(fixture)) is AgentCoreCliReader


def test_prompt_file_browser_memory_and_default_tools():
    [pulse] = records("agentcore-cli/the-pulse").values()
    spec = pulse.spec
    assert (pulse.agent, pulse.platform) == ("thepulse/thepulse", "bedrock-agentcore")
    assert (spec.model.requested.value, spec.model.provider.value) == ("global.anthropic.claude-sonnet-4-6", "anthropic")
    # No inline systemPrompt: the instructions are system-prompt.md, next to harness.json.
    assert spec.instructions.basis == "app/thepulse/system-prompt.md"
    assert spec.instructions.text.startswith("You are The Pulse")
    tools = {t.ref: t.scope.value for t in spec.tools}
    # No allowedTools: every session also has the shell and file tools.
    assert tools == {"tool/bedrock-agentcore.browser": Scope.EXECUTE, "tool/bedrock-agentcore.shell": Scope.EXECUTE,
                     "tool/bedrock-agentcore.file_operations": Scope.WRITE}
    ext = spec.extensions["bedrock-agentcore"]
    assert ext["memory"] == {"AgentCoreMemoryConfiguration": {"Name": "thepulseMemory"}}
    assert (ext["max_iterations"], ext["timeout_seconds"]) == (75, 1800)
    # No executionRoleArn: agentcore deploy creates the role.
    assert spec.permissions.identity.value is IdentityKind.SERVICE_PRINCIPAL
    assert spec.permissions.identity.provenance is Provenance.DERIVED


def test_inline_prompts_inline_functions_and_allowed_tools():
    harnesses = records("agentcore-cli/3d-rams")
    assert len(harnesses) == 10
    hazard = harnesses["RamsAgent/rams_hazard_harness"].spec
    # harness.json sets systemPrompt inline, so the CLI ignores system-prompt.md.
    assert hazard.instructions.basis == "systemPrompt"
    [tool] = hazard.tools
    assert tool.ref == "tool/bedrock-agentcore.RamsAgent.rams_hazard_harness.extract_hazard_notes"
    assert tool.input_schema["required"] == ["features"]
    # allowedTools lists only the inline function: no shell or file tools.
    assert hazard.extensions["bedrock-agentcore"]["allowed_tools"] == ["extract_hazard_notes"]
    supervisor = harnesses["RamsAgent/rams_supervisor_harness"].spec
    assert supervisor.tools == []
    assert supervisor.extensions["bedrock-agentcore"]["model_config"] == {"type": "BedrockModelConfig", "ApiFormat": "converse_stream"}
    # Environment variables are recorded by name.
    assert "TAVILY_MODE" in harnesses["RamsAgent/rams_open_web_harness"].spec.extensions["bedrock-agentcore"]["environment_variables"]


def test_skills_container_and_code_interpreter():
    [ppt] = records("agentcore-cli/ppt-agent").values()
    spec = ppt.spec
    assert spec.model.parameters == {"max_tokens": 64000}
    assert "tool/bedrock-agentcore.code_interpreter" in {t.ref for t in spec.tools}
    ext = spec.extensions["bedrock-agentcore"]
    assert ext["skills"] == [{"Path": "/opt/skills/pptx"}]
    assert ext["dockerfile"] == "Dockerfile"
    assert ext["environment"] == {"AgentCoreRuntimeEnvironment": {"FilesystemConfigurations": [{"SessionStorage": {"MountPath": "/mnt/data"}}]}}
