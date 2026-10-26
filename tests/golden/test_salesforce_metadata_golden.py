"""Golden tests for legacy Agentforce metadata, against Salesforce's public Coral Cloud sample app."""

from pathlib import Path

import pytest

from marshal_ledger.model import IdentityKind, Provenance
from marshal_ledger.readers.salesforcemetadata import SalesforceMetadataReader

CACHE = Path(__file__).resolve().parent.parent / "fixtures" / ".cache"


def records():
    path = CACHE / "salesforce-metadata/coral-cloud"
    if not path.is_dir():
        pytest.skip("fixture not fetched; run python scripts/fetch_fixtures.py")
    return {r.agent: r for r in SalesforceMetadataReader().read(path).records}


def test_service_agent():
    agent = records()["Coral_Cloud_Agent"]
    spec = agent.spec
    assert spec.display_name.value == "Coral Cloud Service Agent"
    assert spec.permissions.identity.value is IdentityKind.SERVICE_PRINCIPAL
    assert spec.permissions.principal.provenance is Provenance.UNKNOWN  # a per-org placeholder
    assert spec.instructions.provenance is Provenance.DERIVED
    assert "Never reveal the verification code" in spec.instructions.text
    assert set(spec.extensions["salesforce-metadata"]["topics"]) >= {"Verified_Customer_Support", "Unverified_Customer_Support"}
    tools = {t.ref: t for t in spec.tools}
    assert tools["tool/salesforce.flow.Create_Booking"].requires_confirmation.value is True
    assert tools["tool/salesforce.flow.Create_Booking"].output_schema["properties"]["createdBooking"]["type"] == "object"
    # An action defined in the other app's package still resolves.
    assert "tool/salesforce.apex.CheckWeather" in tools


def test_employee_agent_runs_as_the_user():
    agent = records()["Coral_Cloud_Employee_Agent"]
    assert agent.spec.permissions.identity.value is IdentityKind.ON_BEHALF_OF_USER
    weather = {t.ref: t for t in agent.spec.tools}["tool/salesforce.apex.CheckWeather"]
    assert weather.input_schema["required"] == ["dateToCheck"]
