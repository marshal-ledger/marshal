import re
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from marshal_ledger.model import AgentSpec, Channel, DeploymentEvent, Grant, Scope, Source, ToolUse, Value
from marshal_ledger.readers.base import build_record
from marshal_ledger.store import Store, StoreUnavailable
from marshal_ledger.web import WebSettings, create_web_app, export_snapshot

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


def test_an_unreachable_store_is_503(db, monkeypatch):
    def unreachable(*args, **kwargs):
        raise StoreUnavailable("cannot connect to the Postgres store: db.internal refused")

    monkeypatch.setattr("marshal_ledger.web.Store", unreachable)
    client = TestClient(create_web_app(db, WebSettings(enabled=True, allow_unauthenticated=True)))
    response = client.get("/")
    assert (response.status_code, response.headers["Retry-After"]) == (503, "5")
    assert "db.internal" not in response.text


def test_static_snapshot_links_every_page_to_a_file(db, tmp_path):
    out = tmp_path / "site"
    count = export_snapshot(db, out)
    files = {p.name for p in out.iterdir()}
    assert count == len(files) - 1 and {"index.html", "deps.html", "favicon.svg"} <= files
    for page in out.glob("*.html"):
        text = page.read_text()
        assert "<form" not in text and "A static snapshot" in text
        assert "http" not in text.split("<main>")[0], "no external resources"
        for href in re.findall(r'href="([^"]*)"', text):
            assert (out / ("index.html" if href == "./" else href)).exists(), f"{page.name} links to {href}"
    [agent] = [p for p in out.glob("agent-finance-invoice-*.html") if "-v" not in p.name.split("invoice")[1]]
    assert "Changes from shipped v1" in agent.read_text()
    deps = (out / "deps.html").read_text()
    assert "tool/sap.update_po" in deps
    [update_po] = re.findall(r'href="(deps-[^"]*update-po[^"]*)"', deps)
    assert "finance/invoice" in (out / update_po).read_text()


def test_snapshot_pages_have_titles_descriptions_and_a_sitemap(db, tmp_path):
    out = tmp_path / "site"
    export_snapshot(db, out, site_url="https://example.com/demo")
    index = (out / "index.html").read_text()
    assert "<title>AI agent inventory · Marshal agent ledger</title>" in index
    assert '<meta name=description content="An AI agent inventory of ' in index
    sitemap = (out / "sitemap.xml").read_text()
    locs = re.findall(r"<loc>([^<]*)</loc>", sitemap)
    assert locs[:2] == ["https://example.com/demo/", "https://example.com/demo/deps.html"]
    agents = [loc.rsplit("/", 1)[1] for loc in locs[2:]]
    assert agents and all(name.startswith("agent-") and (out / name).exists() for name in agents)
    assert len(agents) == len(set(agents))
    page = (out / agents[0]).read_text()
    assert " agent) · Marshal agent ledger</title>" in page and " on " in page.split("<meta name=description", 1)[1]
    export_snapshot(db, tmp_path / "plain")
    assert not (tmp_path / "plain" / "sitemap.xml").exists()
