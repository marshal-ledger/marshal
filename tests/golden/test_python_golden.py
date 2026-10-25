"""Golden tests for the Python reader against real, public agent code."""

from pathlib import Path

import pytest

from marshal_ledger.model import Provenance
from marshal_ledger.readers import ReadOptions
from marshal_ledger.readers.python import PythonAgentReader

CACHE = Path(__file__).resolve().parent.parent / "fixtures" / ".cache"


def read(fixture_id: str):
    path = CACHE / fixture_id
    if not path.is_dir():
        pytest.skip(f"fixture {fixture_id} not fetched; run python scripts/fetch_fixtures.py")
    if fixture_id == "python/crewai-template":
        # The official template is rendered by `crewai create crew`; do the same substitution.
        crew = path / "crew.py"
        crew.write_text(crew.read_text().replace("{{crew_name}}", "ResearchCrew"))
    return PythonAgentReader().read(path, ReadOptions())


def by_name(result):
    return {r.spec.display_name.value: r for r in result.records}


def test_openai_agents_examples():
    agents = by_name(read("python/openai-agents"))
    # Handoffs and agents-as-tools resolve to the agents they name.
    orchestrator = agents["orchestrator_agent"]
    assert {t.ref for t in orchestrator.spec.tools} == {
        "agent/openai-agents/spanish-agent", "agent/openai-agents/french-agent", "agent/openai-agents/italian-agent"
    }
    # An f-string over an SDK constant keeps the constant as a named placeholder.
    faq = agents["FAQ Agent"]
    assert faq.spec.instructions.provenance is Provenance.DERIVED
    assert faq.spec.instructions.text.startswith("{RECOMMENDED_PROMPT_PREFIX}")
    [lookup] = faq.spec.tools
    assert (lookup.name, lookup.description) == ("faq_lookup_tool", "Lookup frequently asked questions.")
    # Docstrings and signatures become tool descriptions and input schemas.
    seat = agents["Seat Booking Agent"].spec.tools[0]
    assert seat.description == "Update the seat for a given confirmation number."
    assert seat.input_schema["required"] == ["confirmation_number", "new_seat"]
    # Hosted tools and declared models.
    search = agents["FinancialSearchAgent"]
    assert [t.ref for t in search.spec.tools] == ["tool/openai.web_search"]
    assert search.spec.model.provider.value == "openai"
    assert agents["FinancialPlannerAgent"].spec.model.requested.value == "o3-mini"


def test_google_adk_sample():
    result = read("python/google-adk")
    agents = by_name(result)
    critic = agents["critic_agent"]
    # The prompt lives in a sibling module, imported with `from . import prompt`.
    assert critic.spec.instructions.provenance is Provenance.DECLARED
    assert "investigative journalist" in critic.spec.instructions.text
    assert [t.ref for t in critic.spec.tools] == ["tool/google.search"]
    # The model comes from an environment variable: unknown, and says so.
    assert critic.spec.model.requested.provenance is Provenance.UNKNOWN
    assert "MODEL_NAME" in critic.spec.model.requested.basis
    assert [s.reason for s in result.skipped] == ["ADK workflow agent; its sub-agents are recorded"]


def test_crewai_template():
    result = read("python/crewai-template")
    agents = {r.agent: r for r in result.records}
    researcher = agents["crewai-template/researcher"]
    assert researcher.spec.instructions.provenance is Provenance.DERIVED
    assert researcher.spec.instructions.text.startswith("Role: {topic} Senior Data Researcher")
    assert researcher.spec.description.value.startswith("Uncover cutting-edge developments")
    assert researcher.spec.extensions["python"]["config"] == "config/agents.yaml#researcher"


def test_raw_api_agent_loop():
    result = read("python/anthropic-quickstarts")
    loop = {r.agent: r for r in result.records}["anthropic-quickstarts/sampling-loop"]
    assert loop.platform == "anthropic-api"
    assert loop.source.detected_by.startswith("inferred:")
    # The system prompt is an f-string over values computed at run time: kept with placeholders.
    assert loop.spec.instructions.provenance is Provenance.DERIVED
    assert "<SYSTEM_CAPABILITY>" in loop.spec.instructions.text
    assert loop.spec.model.requested.provenance is Provenance.UNKNOWN
    assert loop.spec.extensions["python"]["tools_unresolved"]
