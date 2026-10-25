"""Golden tests against real, public Databricks bundles (see tests/fixtures/sources.json)."""

from pathlib import Path

import pytest

from marshal_ledger.model import IdentityKind, Provenance, Scope
from marshal_ledger.readers import ReadOptions
from marshal_ledger.readers.databricks import DatabricksBundleReader

CACHE = Path(__file__).resolve().parent.parent / "fixtures" / ".cache"


def fixture(fixture_id: str) -> Path:
    path = CACHE / fixture_id
    if not path.is_dir():
        pytest.skip(f"fixture {fixture_id} not fetched; run python scripts/fetch_fixtures.py")
    return path


def read(fixture_id: str, **options):
    return DatabricksBundleReader().read(fixture(fixture_id), ReadOptions(**options))


def test_genie_space_nyc_taxi():
    result = read("databricks/genie-space-nyc-taxi")
    [record] = result.records
    assert record.agent == "genie_space_nyc_taxi/nyc_taxi_genie"
    assert record.spec.display_name.value == "NYC Taxi Trip Analysis"
    assert record.spec.instructions.provenance is Provenance.DECLARED
    assert "SUM(fare_amount)" in record.spec.instructions.text
    assert [k.ref for k in record.spec.knowledge] == ["knowledge/databricks.uc.table.samples.nyctaxi.trips"]
    assert record.spec.permissions.identity.value is IdentityKind.ON_BEHALF_OF_USER
    assert record.spec.extensions["databricks"]["example_sql_count"] == 1
    assert result.warnings == []


def test_app_with_genie_space_links_app_to_space():
    result = read("databricks/app-with-genie-space")
    by_agent = {r.agent: r for r in result.records}
    assert set(by_agent) == {"app_with_genie_space/nyc_taxi_genie", "app_with_genie_space/genie_assistant"}
    app = by_agent["app_with_genie_space/genie_assistant"]
    assert [t.ref for t in app.spec.tools] == ["agent/app_with_genie_space/nyc_taxi_genie"]
    assert app.spec.tools[0].scope.value is Scope.READ
    assert app.spec.instructions.provenance is Provenance.UNKNOWN


@pytest.mark.parametrize(
    "fixture_id, agent",
    [
        ("databricks/agent-openai-agents-sdk", "agent_openai_agents_sdk/agent_openai_agents_sdk"),
        ("databricks/agent-langgraph-advanced", "agent_langgraph_advanced/agent_langgraph_advanced"),
    ],
)
def test_agent_app_templates(fixture_id, agent):
    result = read(fixture_id)
    [record] = result.records
    assert record.agent == agent
    targets = {g.target: g.privilege for g in record.spec.permissions.grants}
    assert targets["model/databricks.serving_endpoint.databricks-claude-sonnet-5-5"] == "CAN_QUERY"
    assert record.spec.model.requested.provenance is Provenance.UNKNOWN
    assert record.spec.permissions.identity.value is IdentityKind.SERVICE_PRINCIPAL

    prod = read(fixture_id, target="prod").records[0]
    assert prod.version == record.version, "the prod target only renames the app"
