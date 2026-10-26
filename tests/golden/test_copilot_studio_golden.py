"""Golden tests for the Copilot Studio reader against Microsoft's public example workspaces."""

import os
from pathlib import Path

import pytest

from marshal_ledger.convert import convert
from marshal_ledger.model import IdentityKind, Scope
from marshal_ledger.readers.copilotstudio import CopilotStudioReader

CACHE = Path(os.environ.get("MARSHAL_FIXTURE_CACHE") or Path(__file__).resolve().parent.parent / "fixtures" / ".cache")


def records(fixture_id):
    path = CACHE / fixture_id
    if not path.is_dir():
        pytest.skip(f"fixture {fixture_id} not fetched; run python scripts/fetch_fixtures.py")
    return {r.agent: r for r in CopilotStudioReader().read(path).records}


def test_all_the_things_workspace():
    agents = records("copilot-studio/all-the-things")
    agent = agents["test_allthethings"]
    spec = agent.spec
    assert spec.display_name.value == "AllTheThings"
    assert spec.instructions.text.startswith("You are a helpful assistant")
    refs = {t.ref for t in spec.tools}
    assert "tool/powerplatform.shared_msnweather.TodaysForecast" in refs  # connector via connectionreferences.mcs.yml
    assert "tool/powerplatform.flow.4527d3de-ecfe-4498-9036-980d4c95e5e2" in refs  # file wrapped in a code fence
    assert "agent/test_subagent_01" in refs  # connected agent
    assert "agent/test_allthethings/HelperAgent" in refs  # sub-agent, recorded separately
    assert agents["test_allthethings/HelperAgent"].spec.instructions.text.startswith("1. Greet the user.")
    types = {k.ref: k.type for k in spec.knowledge}
    assert types["knowledge/https://contoso.sharepoint.com/sites/hr"] == "sharepoint"
    assert all(k.scope.value is Scope.READ for k in spec.knowledge)
    # An email trigger makes the agent act on its own, with the maker's connection.
    assert spec.extensions["copilot-studio"]["triggers"][0]["connector"] == "shared_office365"
    assert spec.permissions.identity.value is IdentityKind.MIXED
    assert agent.bindings["dataverse_bot_id"]


def test_connection_modes_and_unauthenticated_access():
    [agent] = [r for r in records("copilot-studio/mcp-agent").values()]
    spec = agent.spec
    assert spec.model.requested.value == "GPT5Chat"
    modes = {e["connector"]: e["connection_mode"] for e in spec.extensions["copilot-studio"]["actions"].values()}
    assert modes == {"shared_custommcp": "Maker", "shared_microsoftlearndocsmcpserver": "Invoker"}
    assert spec.permissions.identity.value is IdentityKind.MIXED
    assert "unauthenticated users" in {a.principal for a in spec.permissions.access}


def test_native_round_trip_keeps_tools_knowledge_and_identity():
    agent = records("copilot-studio/all-the-things")["test_allthethings"]
    conversion = convert(agent, "copilot-studio")
    back = conversion.read_back
    assert back.agent == agent.agent
    assert {t.ref for t in back.spec.tools} >= {
        "tool/powerplatform.shared_msnweather.TodaysForecast",
        "tool/powerplatform.flow.4527d3de-ecfe-4498-9036-980d4c95e5e2",
        "agent/test_subagent_01",
    }
    assert {k.ref for k in back.spec.knowledge} == {k.ref for k in agent.spec.knowledge}
    assert all(f.note for f in conversion.report.lossy)


def test_solution_wellness():
    """An unpacked solution (pac solution unpack) from Microsoft's Power Platform templates."""
    [agent] = records("copilot-studio/solution-wellness").values()
    spec = agent.spec
    assert agent.agent == "mpa_wellnessCopilot"
    assert agent.source.detected_by == "Copilot Studio solution"
    assert spec.display_name.value == "Wellness Copilot"
    assert spec.instructions.text.startswith("You are a wellness advisor")  # the GPT component, not CopilotInstructions
    assert {k.ref for k in spec.knowledge} == {"knowledge/https://www.cdc.gov", "knowledge/https://www.nccih.nih.gov/"}
    refs = {t.ref for t in spec.tools}
    assert "tool/powerplatform.ai_plugin.msdynaib_GoalWritingTipsV2" in refs  # a prompt action
    assert any(r.startswith("tool/powerplatform.flow.") for r in refs)  # a flow called from a topic
    # The flow's connections aren't in the agent definition, so identity isn't guessed.
    assert spec.permissions.identity.value is None
    settings = spec.extensions["copilot-studio"]["settings"]
    assert (settings["authenticationMode"], settings["authenticationTrigger"]) == ("Integrated", "Always")
    assert spec.extensions["copilot-studio"]["topics"]["Greeting"]["trigger"] == "OnRecognizedIntent"


def test_solution_topic_calls_and_connection_references():
    [agent] = records("copilot-studio/solution-leave-management").values()
    spec = agent.spec
    refs = {t.ref for t in spec.tools}
    assert {
        "tool/powerplatform.shared_commondataserviceforapps.ListRecordsWithOrganization",
        "tool/powerplatform.shared_office365users.DirectReports_V2",
        "tool/powerplatform.shared_office365users.Manager_V2",
    } <= refs
    calls = spec.extensions["copilot-studio"]["topic_calls"]
    assert calls["tool/powerplatform.shared_office365users.Manager_V2"]["credentials"] == "end user"
    assert len(agent.bindings["connection_references"]) == 4


def test_solution_zip_matches_unpacked(tmp_path):
    import zipfile

    source = CACHE / "copilot-studio/solution-wellness"
    if not source.is_dir():
        pytest.skip("fixture copilot-studio/solution-wellness not fetched")
    archive = tmp_path / "Wellness_1_0_0_27.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for path in source.rglob("*"):
            if path.is_file():
                z.write(path, path.relative_to(source).as_posix())
    [unpacked] = records("copilot-studio/solution-wellness").values()
    [packed] = CopilotStudioReader().read(archive).records
    assert packed.version == unpacked.version
