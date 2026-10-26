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
