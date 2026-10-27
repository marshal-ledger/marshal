import pytest
from fastapi.testclient import TestClient

from conftest import AZURE_DEVOPS, GITHUB, GITLAB, JENKINS
from marshal_ledger.hashing import sha256_text
from marshal_ledger.model import AgentSpec, Instructions, Provenance, Source, ToolUse
from marshal_ledger.oidc import IssuerConfig
from marshal_ledger.readers.base import build_record
from marshal_ledger.server import ServerConfig, create_app
from marshal_ledger.store import Store


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


@pytest.fixture
def ci_config(ledger_location):
    return ServerConfig(
        store=ledger_location,
        issuers=[
            IssuerConfig(
                issuer=JENKINS, provider="jenkins", owner="parent", repositories=["https://jenkins.example.com/job/agents/*"]
            ),
            IssuerConfig(issuer=AZURE_DEVOPS, provider="azure-devops", owner="parent", repositories=["acme/agents"]),
        ],
        policy={
            "shipped": {
                "workflows": ["https://jenkins.example.com/job/agents/job/*/job/main/", "sc://acme/agents/marshal-prod"]
            }
        },
    )


def test_jenkins_branch_jobs_propose_and_the_main_job_ships(ci_config, signer):
    client = TestClient(create_app(ci_config, key_resolver=signer.resolver))
    pr_job = signer.jenkins(sub="https://jenkins.example.com/job/agents/job/finance/job/PR-7/")
    assert client.post("/v0/events", json=body("proposed"), headers=auth(pr_job)).status_code == 200
    assert client.post("/v0/events", json=body("shipped"), headers=auth(pr_job)).status_code == 403
    # With owner: parent, both jobs are the multibranch project, so the main job may ship the agent.
    assert client.post("/v0/events", json=body("shipped"), headers=auth(signer.jenkins())).status_code == 200
    other_project = signer.jenkins(sub="https://jenkins.example.com/job/agents/job/hr/job/main/")
    response = client.post("/v0/events", json=body("shipped"), headers=auth(other_project))
    assert response.status_code == 403 and "belongs to" in response.json()["detail"]


def test_azure_devops_pipeline_proposes_and_service_connection_ships(ci_config, signer):
    client = TestClient(create_app(ci_config, key_resolver=signer.resolver))
    pipeline = signer.azure_devops(sub="p://acme/agents/record-agents")
    assert client.post("/v0/events", json=body("proposed"), headers=auth(pipeline)).status_code == 200
    assert client.post("/v0/events", json=body("shipped"), headers=auth(pipeline)).status_code == 403
    prod = signer.azure_devops(sub="sc://acme/agents/marshal-prod")
    assert client.post("/v0/events", json=body("shipped"), headers=auth(prod)).status_code == 200
    other = signer.azure_devops(sub="sc://acme/payments/marshal-prod")
    assert client.post("/v0/events", json=body("proposed"), headers=auth(other)).status_code == 403


def test_jenkins_build_number_and_job_are_audited(ci_config, signer):
    client = TestClient(create_app(ci_config, key_resolver=signer.resolver))
    client.post("/v0/events", json=body("shipped"), headers=auth(signer.jenkins(sha="9e8d7c6")))
    with Store(ci_config.store) as store:
        [(event, _, actor)] = store.history("finance/invoice")
    assert (event.commit, event.pipeline_run) == ("9e8d7c6", "jenkins:42")
    assert actor.repository == "https://jenkins.example.com/job/agents/job/finance/"
    assert actor.workflow == "https://jenkins.example.com/job/agents/job/finance/job/main/"
