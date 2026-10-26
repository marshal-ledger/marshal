import json

import pytest

from marshal_ledger.model import AgentSpec, IdentityKind, KnowledgeUse, Source, ToolUse, Value
from marshal_ledger.portability import LEVELS, assess, load_matrix, platforms
from marshal_ledger.readers.base import build_record
from marshal_ledger.writers import WRITERS


@pytest.mark.parametrize("platform", platforms())
def test_matrix_files_are_well_formed(platform):
    matrix = load_matrix(platform)
    assert matrix.platform == platform
    assert matrix.writer is None or matrix.writer in WRITERS
    assert matrix.sources, "every matrix cites the platform documentation"
    for rules in (matrix.tools, matrix.knowledge, matrix.identity, matrix.models):
        assert rules and rules[-1].pattern == "*", "each list ends with a default rule"
        for rule in rules:
            assert rule.level in LEVELS
            if rule.level != "native":
                assert rule.advice, f"{platform}: {rule.pattern} needs advice"


def record(tools=(), knowledge=(), provider="openai", identity=IdentityKind.SERVICE_PRINCIPAL):
    spec = AgentSpec(
        model={"requested": Value[str].declared("gpt-5"), "provider": Value[str].declared(provider)},
        permissions={"identity": Value[IdentityKind].declared(identity)},
        tools=[ToolUse(ref=r, name=r.rsplit(".", 1)[-1]) for r in tools],
        knowledge=[KnowledgeUse(ref=r) for r in knowledge],
    )
    return build_record(agent="team/agent", platform="test", source=Source(format="test", artifact="a"), spec=spec)


def test_limitations_come_from_the_matrix():
    report = assess(record(tools=["tool/bedrock.agent.group.op"], knowledge=["knowledge/bedrock.knowledge_base.kb1"],
                           provider="anthropic"), "bedrock")
    assert report.limitations == []
    moved = assess(record(knowledge=["knowledge/databricks.uc.vector_index.main.docs"], provider="google"), "bedrock")
    levels = {i.element: i.level for i in moved.limitations}
    assert levels["model"] == "none"
    assert levels["knowledge:knowledge/databricks.uc.vector_index.main.docs"] == "adapt"
    assert moved.score < report.score


def test_unknowns_are_listed_not_scored():
    base = record(tools=["tool/powerplatform.shared_office365.SendEmail"], provider="azure-openai",
                  identity=IdentityKind.ON_BEHALF_OF_USER)
    report = assess(base, "copilot-studio")
    assert any("scope unknown" in u for u in report.unknowns)
    assert report.limitations == []


def test_agent_specific_issues_come_from_conversion():
    # An Agent Script action name must be an API name, so the tool name is approximated: an agent-specific issue.
    report = assess(record(tools=["tool/salesforce.flow.Lookup Order"], provider="openai"), "agentforce")
    [issue] = [i for i in report.agent_issues if i.element == "tool:tool/salesforce.flow.Lookup Order"]
    assert "API names" in issue.message
    assert not [i for i in report.limitations if i.element == issue.element], "a native tool is not a platform limitation"


def test_cli_port(tmp_path, capsys):
    from marshal_ledger.cli import main

    adl = tmp_path / "a.adl.yaml"
    adl.write_text(
        "adl_spec: '0.3.0'\nname: Calculator\ndescription: Adds numbers.\nversion: '1.0.0'\n"
        "data_classification: {sensitivity: public}\nmodel: {provider: anthropic, name: claude-sonnet}\n"
        "tools: [{name: add, description: Add numbers., read_only: true}]\n"
    )
    assert main(["port", str(adl), "--to", "bedrock", "--db", str(tmp_path / "x.sqlite")]) == 0
    out = capsys.readouterr().out
    assert "Portability:" in out and "Amazon Bedrock Agents" in out
    assert main(["port", str(adl), "--to", "all", "--output", "json", "--db", str(tmp_path / "x.sqlite")]) == 0
    assert {r["target"] for r in json.loads(capsys.readouterr().out)} == set(platforms())


def test_an_agent_on_the_target_platform_is_current():
    agent = record(tools=("tool/custom.thing",))
    on_platform = agent.model_copy(update={"platform": "copilot-studio"})
    report = assess(on_platform, "copilot-studio")
    assert report.current and report.score == 100 and not report.limitations
    assert "Already on Microsoft Copilot Studio" in report.text()
    assert report.to_dict()["current"] is True
    assert not assess(agent, "copilot-studio").current
