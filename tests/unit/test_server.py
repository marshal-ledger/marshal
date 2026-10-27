import pytest
from fastapi.testclient import TestClient

from conftest import GITHUB, GITLAB, without_password
from marshal_ledger.hashing import sha256_text
from marshal_ledger.model import AgentSpec, Instructions, Provenance, Source, ToolUse
from marshal_ledger.oidc import IssuerConfig
from marshal_ledger.readers.base import build_record
from marshal_ledger.server import ServerConfig, create_app
from marshal_ledger.store import Store, StoreUnavailable
from marshal_ledger.web import WebSettings


def record(agent="finance/invoice", tools=()):
    text = "Reconcile invoices."
    spec = AgentSpec(
        instructions=Instructions(hash=sha256_text(text), text=text, provenance=Provenance.DECLARED),
        tools=[ToolUse(ref=t) for t in tools],
    )
    return build_record(agent=agent, platform="test", source=Source(format="test", artifact="a.yml"), spec=spec)


def body(event="proposed", environment="prod", records=None):
    records = records if records is not None else [record()]
    return {
        "event": event,
        "environment": environment,
        "records": [r.model_dump(mode="json", by_alias=True) for r in records],
    }


@pytest.fixture
def config(ledger_location):
    return ServerConfig(
        store=ledger_location,
        issuers=[
            IssuerConfig(issuer=GITHUB, provider="github", repositories=["acme/*"]),
            IssuerConfig(issuer=GITLAB, provider="gitlab", repositories=["acme/*"]),
        ],
        policy={"shipped": {"refs": ["refs/heads/main"], "environments": ["prod"], "protected_refs": True}},
    )


@pytest.fixture
def client(config, signer):
    return TestClient(create_app(config, key_resolver=signer.resolver))


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_health(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_missing_or_bad_token_is_401(client):
    assert client.post("/v0/events", json=body()).status_code == 401
    assert client.post("/v0/events", json=body(), headers=auth("junk")).status_code == 401


def test_repository_outside_allowlist_is_403(client, signer):
    response = client.post("/v0/events", json=body(), headers=auth(signer.github(repository="evil/agents")))
    assert response.status_code == 403


def test_pull_request_may_propose_but_not_ship(client, signer):
    pr = signer.github(ref="refs/pull/12/merge", event_name="pull_request")
    assert client.post("/v0/events", json=body("proposed"), headers=auth(pr)).status_code == 200
    response = client.post("/v0/events", json=body("shipped"), headers=auth(pr))
    assert response.status_code == 403
    assert "shipped policy" in response.json()["detail"]


def test_ship_from_main_or_protected_environment(client, signer, config):
    assert client.post("/v0/events", json=body("shipped"), headers=auth(signer.github())).status_code == 200
    release = signer.github(ref="refs/heads/release", environment="prod")
    assert client.post("/v0/events", json=body("shipped"), headers=auth(release)).status_code == 200
    gitlab = signer.gitlab(project_path="acme/finance-agents", ref="hotfix")
    response = client.post("/v0/events", json=body("shipped"), headers=auth(gitlab))
    assert response.status_code == 403, "same repository path under another issuer is a different owner"


def test_agents_are_bound_to_their_first_repository(client, signer):
    assert client.post("/v0/events", json=body(), headers=auth(signer.github())).status_code == 200
    other = signer.github(repository="acme/other-team")
    response = client.post("/v0/events", json=body(), headers=auth(other))
    assert response.status_code == 403
    assert "belongs to acme/finance-agents" in response.json()["detail"]


def test_response_reports_ranked_changes(client, signer):
    client.post("/v0/events", json=body("shipped"), headers=auth(signer.github()))
    response = client.post(
        "/v0/events",
        json=body("proposed", records=[record(tools=["tool/sap.update_po"])]),
        headers=auth(signer.github(ref="refs/pull/3/merge")),
    )
    [report] = response.json()["reports"]
    assert (report["baseline"], report["label"]) == ("v1", "v2")
    assert report["changes"][0]["severity"] == "HIGH"


def test_token_claims_override_commit_and_run_and_are_audited(client, signer, config):
    sent = record()
    sent.source.commit = "client-claimed"
    client.post("/v0/events", json=body("shipped", records=[sent]), headers=auth(signer.github()))
    with Store(config.store) as store:
        [(event, _, actor)] = store.history("finance/invoice")
    assert event.commit == "3f9c2e1"
    assert event.pipeline_run == "github:8812/1"
    assert (actor.repository, actor.ref) == ("acme/finance-agents", "refs/heads/main")


def test_hash_only_server_never_stores_text(config, signer):
    config.hash_only = True
    client = TestClient(create_app(config, key_resolver=signer.resolver))
    client.post("/v0/events", json=body(), headers=auth(signer.github()))
    with Store(config.store) as store:
        stored = store.resolve("finance/invoice", "latest").record
    assert stored.spec.instructions.text is None
    assert stored.spec.instructions.hash == sha256_text("Reconcile invoices.")


def test_malformed_body_is_422(client, signer):
    response = client.post("/v0/events", json={"event": "deployed"}, headers=auth(signer.github()))
    assert response.status_code == 422


def test_config_loads_from_yaml(tmp_path):
    path = tmp_path / "marshal-server.yaml"
    path.write_text(
        "store: /var/lib/marshal/ledger.sqlite\n"
        "issuers:\n"
        "  - issuer: https://token.actions.githubusercontent.com\n"
        "    provider: github\n"
        "    repositories: ['acme/*']\n"
    )
    config = ServerConfig.load(path)
    assert config.policy.shipped.refs == ["refs/heads/main"]
    assert config.issuers[0].audience == "marshal"


def test_observed_events_need_the_shipped_policy_and_are_recorded_as_scheduled(client, signer, config):
    pr = signer.github(ref="refs/pull/9/merge")
    assert client.post("/v0/events", json=body("observed"), headers=auth(pr)).status_code == 403
    assert client.post("/v0/events", json=body("shipped"), headers=auth(signer.github())).status_code == 200
    response = client.post("/v0/events", json=body("observed"), headers=auth(signer.github()))
    assert response.json()["reports"][0]["channel"] == "scheduled"
    with Store(config.store) as store:
        assert [e.channel.value for e, _, _ in store.history("finance/invoice")] == ["ci", "scheduled"]


def test_an_unreachable_store_is_503_with_retry_after(client, signer, monkeypatch):
    def unreachable(*args, **kwargs):
        raise StoreUnavailable("cannot connect to the Postgres store: db.internal refused")

    monkeypatch.setattr("marshal_ledger.server.Store", unreachable)
    response = client.post("/v0/events", json=body(), headers=auth(signer.github()))
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert "db.internal" not in response.text


def test_store_auth_needs_a_postgres_store(config):
    config = config.model_copy(update={"store": "ledger.sqlite", "store_auth": "azure"})
    with pytest.raises(ValueError, match="postgresql"):
        create_app(config)


def test_store_auth_supplies_the_postgres_password(config, signer, ledger_location):
    if not ledger_location.startswith("postgresql://"):
        pytest.skip("needs Postgres (set MARSHAL_TEST_POSTGRES_URL)")
    config = config.model_copy(update={
        "store": without_password(ledger_location),
        "store_auth": "conftest:postgres_password",
        "web": WebSettings(enabled=True),
    })
    client = TestClient(create_app(config, key_resolver=signer.resolver))
    assert client.post("/v0/events", json=body(), headers=auth(signer.github())).status_code == 200
    page = client.get("/v0/agents", headers={"X-Forwarded-Email": "ana@acme.example"})
    assert [a["agent"] for a in page.json()] == ["finance/invoice"]
