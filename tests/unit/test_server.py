from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import AZURE_DEVOPS, GITHUB, GITLAB, JENKINS, without_password
from marshal_ledger.hashing import sha256_text
from marshal_ledger.model import AgentSpec, Instructions, Provenance, Scope, Source, ToolUse, Value
from marshal_ledger.oidc import IssuerConfig
from marshal_ledger.readers.base import build_record
from marshal_ledger.risk import RiskRules
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


@pytest.mark.parametrize("web", [False, True])
def test_no_unauthenticated_api_docs(config, signer, web):
    """FastAPI's docs pages would be unauthenticated and load scripts from a CDN."""
    config.web.enabled = web
    client = TestClient(create_app(config, key_resolver=signer.resolver))
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert client.get(path).status_code == 404, path
    assert client.get("/healthz").status_code == 200


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


def per_environment_client(ledger_location, signer, shipped, issuers=None):
    config = ServerConfig(
        store=ledger_location,
        issuers=issuers
        or [
            IssuerConfig(issuer=GITHUB, provider="github", repositories=["acme/*"]),
            IssuerConfig(issuer=GITLAB, provider="gitlab", repositories=["acme/*"]),
        ],
        policy={"shipped": shipped},
    )
    return TestClient(create_app(config, key_resolver=signer.resolver))


def test_environments_without_an_entry_use_the_top_level_rule(ledger_location, signer):
    client = per_environment_client(
        ledger_location, signer, {"by_environment": {"prod": {"environments": ["prod"]}}}
    )
    main = signer.github()
    assert client.post("/v0/events", json=body("shipped", environment="test"), headers=auth(main)).status_code == 200
    assert client.post("/v0/events", json=body("observed", environment="dev"), headers=auth(main)).status_code == 200
    pr = signer.github(ref="refs/pull/4/merge")
    assert client.post("/v0/events", json=body("shipped", environment="test"), headers=auth(pr)).status_code == 403


def test_an_environment_entry_replaces_the_top_level_rule(ledger_location, signer):
    client = per_environment_client(
        ledger_location,
        signer,
        {"refs": ["refs/heads/main"], "by_environment": {"prod": {"environments": ["prod"]}}},
    )
    main = signer.github()
    response = client.post("/v0/events", json=body("shipped", environment="prod"), headers=auth(main))
    assert response.status_code == 403, "the main branch matches the top-level rule, not prod's"
    detail = response.json()["detail"]
    assert "shipped events for prod" in detail and "shipped rule for prod" in detail
    response = client.post("/v0/events", json=body("observed", environment="prod"), headers=auth(main))
    assert response.status_code == 403
    approved = signer.github(environment="prod")
    assert client.post("/v0/events", json=body("shipped", environment="prod"), headers=auth(approved)).status_code == 200
    staging = signer.github(ref="refs/heads/release", environment="staging")
    assert client.post("/v0/events", json=body("shipped", environment="prod"), headers=auth(staging)).status_code == 403
    # Proposals are never subject to the shipped policy.
    pr = signer.github(ref="refs/pull/4/merge")
    assert client.post("/v0/events", json=body("proposed", environment="prod"), headers=auth(pr)).status_code == 200


def test_an_empty_environment_entry_lets_no_one_record_it(ledger_location, signer):
    client = per_environment_client(ledger_location, signer, {"by_environment": {"prod": {}}})
    response = client.post("/v0/events", json=body("shipped", environment="prod"), headers=auth(signer.github()))
    assert response.status_code == 403


def test_gitlab_protected_environment_for_prod_only(ledger_location, signer):
    client = per_environment_client(
        ledger_location,
        signer,
        {"protected_refs": True, "by_environment": {"prod": {"protected_environments": True}}},
    )
    branch = signer.gitlab()
    assert client.post("/v0/events", json=body("shipped", environment="test"), headers=auth(branch)).status_code == 200
    assert client.post("/v0/events", json=body("shipped", environment="prod"), headers=auth(branch)).status_code == 403
    unprotected = signer.gitlab(environment="production", environment_protected="false")
    response = client.post("/v0/events", json=body("shipped", environment="prod"), headers=auth(unprotected))
    assert response.status_code == 403
    protected = signer.gitlab(environment="production", environment_protected="true")
    assert client.post("/v0/events", json=body("shipped", environment="prod"), headers=auth(protected)).status_code == 200


def test_jenkins_and_azure_devops_workflows_per_environment(ledger_location, signer):
    client = per_environment_client(
        ledger_location,
        signer,
        {
            "workflows": ["https://jenkins.example.com/job/agents/job/*/job/main/", "sc://acme/agents/marshal-*"],
            "by_environment": {
                "prod": {
                    "workflows": ["https://jenkins.example.com/job/agents/job/*/job/release/", "sc://acme/agents/marshal-prod"]
                }
            },
        },
        issuers=[
            IssuerConfig(
                issuer=JENKINS, provider="jenkins", owner="parent", repositories=["https://jenkins.example.com/job/agents/*"]
            ),
            IssuerConfig(issuer=AZURE_DEVOPS, provider="azure-devops", owner="parent", repositories=["acme/agents"]),
        ],
    )
    main_job = signer.jenkins()
    release_job = signer.jenkins(sub="https://jenkins.example.com/job/agents/job/finance/job/release/")
    assert client.post("/v0/events", json=body("shipped", environment="test"), headers=auth(main_job)).status_code == 200
    assert client.post("/v0/events", json=body("shipped", environment="prod"), headers=auth(main_job)).status_code == 403
    assert client.post("/v0/events", json=body("shipped", environment="prod"), headers=auth(release_job)).status_code == 200
    test_connection = signer.azure_devops(sub="sc://acme/agents/marshal-test")
    prod_connection = signer.azure_devops(sub="sc://acme/agents/marshal-prod")
    record_ = body("shipped", environment="test", records=[record(agent="hr/onboarding")])
    assert client.post("/v0/events", json=record_, headers=auth(test_connection)).status_code == 200
    record_["environment"] = "prod"
    assert client.post("/v0/events", json=record_, headers=auth(test_connection)).status_code == 403
    assert client.post("/v0/events", json=record_, headers=auth(prod_connection)).status_code == 200


def test_environment_entries_reject_unknown_keys(tmp_path):
    path = tmp_path / "marshal-server.yaml"
    path.write_text(
        "store: /var/lib/marshal/ledger.sqlite\n"
        "issuers:\n"
        "  - issuer: https://token.actions.githubusercontent.com\n"
        "    provider: github\n"
        "    repositories: ['acme/*']\n"
        "policy:\n"
        "  shipped:\n"
        "    by_environment:\n"
        "      prod:\n"
        "        enviroments: [prod]\n"
    )
    with pytest.raises(ValueError, match="enviroments"):
        ServerConfig.load(path)
    path.write_text(path.read_text().replace("by_environment", "by_environments").replace("        enviroments", "        environments"))
    with pytest.raises(ValueError, match="by_environments"):
        ServerConfig.load(path)


def test_example_configuration_has_a_prod_rule():
    from pathlib import Path

    example = Path(__file__).resolve().parents[2] / "examples" / "marshal-server.yaml"
    config = ServerConfig.load(example)
    prod = config.policy.shipped.rule_for("prod")
    assert prod is not config.policy.shipped and prod.refs == []
    assert config.policy.shipped.rule_for("test") is config.policy.shipped


def test_jenkins_build_number_and_job_are_audited(ci_config, signer):
    client = TestClient(create_app(ci_config, key_resolver=signer.resolver))
    client.post("/v0/events", json=body("shipped"), headers=auth(signer.jenkins(sha="9e8d7c6")))
    with Store(ci_config.store) as store:
        [(event, _, actor)] = store.history("finance/invoice")
    assert (event.commit, event.pipeline_run) == ("9e8d7c6", "jenkins:42")
    assert actor.repository == "https://jenkins.example.com/job/agents/job/finance/"
    assert actor.workflow == "https://jenkins.example.com/job/agents/job/finance/job/main/"


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


RULES = {"rules": [{"name": "erp", "match": {"refs": ["tool/sap.*"]}, "raise_to": "HIGH", "reason": "system of record"},
                   {"name": "routine-instructions", "match": {"categories": ["instructions"]}, "lower_to": "LOW",
                    "reason": "prompt edits are reviewed in the agent repo"}]}


def read_only(proposed):
    for tool in proposed.spec.tools:
        tool.scope, tool.reversible = Value[Scope].declared(Scope.READ), Value[bool].declared(True)
    return proposed


def ruled_client(config, signer):
    config.policy.risk = RiskRules.model_validate(RULES)
    return TestClient(create_app(config, key_resolver=signer.resolver))


def test_org_rules_rate_changes_and_are_named_in_the_response(config, signer):
    client = ruled_client(config, signer)
    client.post("/v0/events", json=body("shipped"), headers=auth(signer.github()))
    proposed = read_only(record(tools=["tool/sap.read_po"]))
    text = "Reconcile invoices quickly."
    proposed.spec.instructions = Instructions(hash=sha256_text(text), text=text, provenance=Provenance.DECLARED)
    response = client.post("/v0/events", json=body("proposed", records=[proposed]),
                           headers=auth(signer.github(ref="refs/pull/3/merge")))
    changes = {c["path"]: c for c in response.json()["reports"][0]["changes"]}
    assert {k: changes["tool tool/sap.read_po"][k] for k in ("severity", "builtin_severity", "rule", "reason")} == {
        "severity": "HIGH", "builtin_severity": "LOW", "rule": "erp", "reason": "system of record"}
    assert changes["instructions"]["severity"] == "LOW"
    assert changes["instructions"]["reason"] == "prompt edits are reviewed in the agent repo"


def test_the_web_view_served_with_the_api_uses_the_org_rules(config, signer):
    config.web = WebSettings(enabled=True)
    client = ruled_client(config, signer)
    client.post("/v0/events", json=body("shipped"), headers=auth(signer.github()))
    client.post("/v0/events", json=body("proposed", records=[read_only(record(tools=["tool/sap.read_po"]))]),
                headers=auth(signer.github(ref="refs/pull/3/merge")))
    page = client.get("/agent", params={"id": "finance/invoice"}, headers={"X-Forwarded-Email": "a@acme.example"}).text
    assert "org rule erp: system of record" in page


@pytest.mark.parametrize("extra", [
    {"risk": RULES},
    {"rules": RULES["rules"]},
    {"policy": {"risk": RULES}},
])
def test_clients_cannot_send_ratings_or_rules(client, signer, extra):
    response = client.post("/v0/events", json=body() | extra, headers=auth(signer.github()))
    assert response.status_code == 422


def test_ratings_in_a_record_are_ignored(client, signer):
    """A record is the agent team's own data; nothing in it changes how the server rates it."""
    client.post("/v0/events", json=body("shipped"), headers=auth(signer.github()))
    sent = record(tools=["tool/sap.update_po"])
    sent.spec.extensions["marshal"] = {"risk": {"rules": [{"name": "quiet", "match": {"categories": ["tools"]},
                                                           "lower_to": "LOW", "reason": "trust us"}]},
                                       "severity": "LOW"}
    response = client.post("/v0/events", json=body("proposed", records=[sent]),
                           headers=auth(signer.github(ref="refs/pull/3/merge")))
    changes = {c["path"]: c for c in response.json()["reports"][0]["changes"]}
    assert changes["tool tool/sap.update_po"]["severity"] == "HIGH"
    assert "rule" not in changes["tool tool/sap.update_po"]


def test_the_example_configuration_loads():
    config = ServerConfig.load(Path(__file__).resolve().parents[2] / "examples" / "marshal-server.yaml")
    assert config.policy.risk.approved_models is not None and config.policy.risk.rules


def test_rules_without_a_reason_to_lower_stop_the_server_loading(tmp_path):
    path = tmp_path / "marshal-server.yaml"
    path.write_text(
        "store: ledger.sqlite\n"
        "issuers: [{issuer: 'https://token.actions.githubusercontent.com', provider: github, repositories: ['acme/*']}]\n"
        "policy:\n"
        "  risk:\n"
        "    rules: [{name: quiet, match: {categories: [model]}, lower_to: LOW}]\n"
    )
    with pytest.raises(ValueError, match="needs a reason"):
        ServerConfig.load(path)
