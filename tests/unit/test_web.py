from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from marshal_ledger.model import AgentSpec, Channel, DeploymentEvent, Grant, Scope, Source, ToolUse, Value
from marshal_ledger.readers.base import build_record
from marshal_ledger.store import Store
from marshal_ledger.web import WebSettings, create_web_app

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def record(description="Reconciles invoices", tools=("tool/sap.update_po",)):
    spec = AgentSpec(
        display_name=Value[str].declared("Invoice <Agent>"),
        description=Value[str].declared(description),
        tools=[ToolUse(ref=r, name=r.rsplit(".", 1)[-1], scope=Value[Scope].declared(Scope.WRITE)) for r in tools],
        permissions={"grants": [Grant(target="store/db", privilege="WRITE", scope=Scope.WRITE)]},
    )
    return build_record(agent="finance/invoice", platform="test", source=Source(format="test", artifact="a.yml"), spec=spec)


@pytest.fixture
def db(ledger_location):
    path = ledger_location
    with Store(path) as s:
        s.record([record()], event=DeploymentEvent.SHIPPED, environment="prod", at=T0, channel=Channel.CI)
        s.record([record("edited", tools=("tool/sap.update_po", "tool/sap.delete_po"))],
                 event=DeploymentEvent.OBSERVED, environment="prod", at=T0, channel=Channel.SCHEDULED)
    return str(path)


def test_requires_the_sso_header(db):
    client = TestClient(create_web_app(db, WebSettings(enabled=True)))
    assert client.get("/").status_code == 401
    assert client.get("/", headers={"X-Forwarded-Email": "ana@acme.example"}).status_code == 200


def test_pages_escape_values_and_show_drift(db):
    client = TestClient(create_web_app(db, WebSettings(enabled=True, allow_unauthenticated=True)))
    index = client.get("/").text
    assert "Invoice &lt;Agent&gt;" in index and "<Agent>" not in index
    assert "drift" in index
    page = client.get("/agent", params={"id": "finance/invoice"}).text
    assert "Changes from shipped v1" in page and "tool/sap.delete_po" in page
    assert "Portability" in page and "History" in page
    deps = client.get("/deps", params={"q": "sap.update_po"}).text
    assert "finance/invoice" in deps
    assert client.get("/agent", params={"id": "nobody"}).status_code == 404
    assert "http" not in index.split("<main>")[0].replace('href="./"', ""), "no external resources"


def test_json_api(db):
    client = TestClient(create_web_app(db, WebSettings(enabled=True, allow_unauthenticated=True)))
    [agent] = client.get("/v0/agents").json()
    assert agent["environments"][0]["drift"] is True
    assert client.get("/v0/agent", params={"id": "finance/invoice"}).json()["agent"] == "finance/invoice"
    assert client.get("/v0/deps", params={"q": "*delete_po"}).json()[0]["relation"] == "tool"


def test_served_with_the_ingest_api(db, signer):
    from conftest import GITHUB

    from marshal_ledger.oidc import IssuerConfig
    from marshal_ledger.server import ServerConfig, create_app

    config = ServerConfig(store=db, issuers=[IssuerConfig(issuer=GITHUB, provider="github", repositories=["acme/*"])],
                          web={"enabled": True})
    client = TestClient(create_app(config, key_resolver=signer.resolver))
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/", headers={"X-Forwarded-Email": "ana@acme.example"}).status_code == 200
    assert client.post("/v0/events", json={}).status_code == 401


def test_tools_show_whose_credentials_they_use(ledger_location):
    spec = AgentSpec(
        display_name=Value[str].declared("Helpdesk"),
        tools=[ToolUse(ref="tool/powerplatform.shared_office365.SendEmailV2"), ToolUse(ref="tool/powerplatform.flow.1")],
        extensions={"copilot-studio": {
            "actions": {"SendMail": {"tool": "tool/powerplatform.shared_office365.SendEmailV2", "credentials": "maker"}},
            "topic_calls": {"tool/powerplatform.flow.1": {"topics": ["Greeting"]}},
        }},
    )
    agent = build_record(agent="helpdesk", platform="copilot-studio", source=Source(format="copilot-studio", artifact="a"), spec=spec)
    with Store(ledger_location) as s:
        s.record([agent], event=DeploymentEvent.SHIPPED, environment="prod", at=T0, channel=Channel.CI)
    client = TestClient(create_web_app(str(ledger_location), WebSettings(enabled=True, allow_unauthenticated=True)))
    page = client.get("/agent", params={"id": "helpdesk"}).text
    assert "<th>Credentials</th>" in page and "maker&#x27;s connection" in page
    assert "Current platform" in page  # its own platform isn't scored for portability


def test_no_credentials_column_without_credential_data(db):
    client = TestClient(create_web_app(db, WebSettings(enabled=True, allow_unauthenticated=True)))
    assert "<th>Credentials</th>" not in client.get("/agent", params={"id": "finance/invoice"}).text


def test_serves_the_logo_as_the_favicon(db):
    client = TestClient(create_web_app(db, WebSettings(enabled=True)))
    icon = client.get("/favicon.svg")  # a static asset, so no SSO header needed
    assert icon.status_code == 200 and icon.headers["content-type"] == "image/svg+xml"
    page = client.get("/", headers={"X-Forwarded-Email": "ana@acme.example"}).text
    assert 'rel=icon href="favicon.svg"' in page and 'class="mark"' in page
