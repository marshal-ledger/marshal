"""Entity resolution: refs on different platforms that reach the same API, server or source."""

import json
import textwrap

import pytest
from fastapi.testclient import TestClient

from marshal_ledger.cli import main
from marshal_ledger.model import AgentSpec, DeploymentEvent, Grant, KnowledgeUse, Scope, Source, ToolUse
from marshal_ledger.readers.base import build_record
from marshal_ledger.resolve import mcp_target, normalize_url, targets
from marshal_ledger.store import Store
from marshal_ledger.web import WebSettings, create_web_app

SHIPPED = DeploymentEvent.SHIPPED


@pytest.mark.parametrize("url, normalized", [
    ("https://API.acme.example/refunds/", "https://api.acme.example/refunds"),
    ("https://api.acme.example:443/refunds?dry_run=1#x", "https://api.acme.example/refunds"),
    ("http://api.acme.example:8080/orders/{order_id}", "http://api.acme.example:8080/orders/{}"),
    ("https://api.acme.example/orders/{{task}}/lines/:line", "https://api.acme.example/orders/{}/lines/{}"),
    ("https://api.acme.example/orders/${ORDER}", "https://api.acme.example/orders/{}"),
    ("http://127.0.0.1:HOST_PORT/costs", None),       # a placeholder port
    ("http://localhost:8000/costs", None),            # loopback isn't one shared system
    ("https://${API_HOST}/refunds", None),             # an unresolved host
    ("ftp://files.acme.example/x", None),
])
def test_urls_are_normalized_or_left_unresolved(url, normalized):
    assert normalize_url(url) == normalized


@pytest.mark.parametrize("server, target", [
    ("https://api.githubcopilot.com/mcp/", "mcp:https://api.githubcopilot.com/mcp"),
    ("npx -y @notionhq/notion-mcp-server", "mcp:npm:@notionhq/notion-mcp-server"),
    ("npx --yes @notionhq/notion-mcp-server@1.9.0 --port 3000", "mcp:npm:@notionhq/notion-mcp-server"),
    ("uvx mcp-server-fetch==2025.1", "mcp:pypi:mcp-server-fetch"),
    ("github-mcp-server", None),                       # a bare name proves nothing
    ("python server.py", None),                        # a local command
    ("powerplatform.shared_custommcp", None),
])
def test_mcp_servers_resolve_by_url_or_package(server, target):
    assert mcp_target(server) == target


def record(agent, platform, tools=(), knowledge=(), grants=()):
    spec = AgentSpec(
        tools=[ToolUse(ref=ref, name=name) for ref, name in tools],
        knowledge=[KnowledgeUse(ref=ref) for ref in knowledge],
        permissions={"grants": [Grant(target=t, privilege="connect", scope=Scope.READ) for t in grants]},
    )
    return build_record(agent=agent, platform=platform, source=Source(format=platform, artifact="a"), spec=spec)


SPEC = record("refunds-spec", "agent-spec", tools=[
    ("tool/http.POST.https://api.acme.example/refunds", "issue_refund"),
    ("tool/mcp.https://mcp.acme.example/mcp/.search_orders", "search_orders"),
])
COPILOT = record("refunds-copilot", "copilot-studio", tools=[("tool/http.POST.https://API.acme.example/refunds/", None)],
                 knowledge=["knowledge/https://contoso.sharepoint.com/sites/hr/"])
ADK = record("orders-adk", "google-adk", tools=[("tool/mcp.https://mcp.acme.example/mcp", "McpToolset")])
ADL = record("ledger-adl", "adl", grants=["other/network.api.acme.example"])
HR = record("hr-adl", "adl", knowledge=["knowledge/https://contoso.sharepoint.com/sites/hr"])


def test_targets_of_a_version():
    assert {(t.ref, t.target) for t in targets(SPEC)} == {
        ("tool/http.POST.https://api.acme.example/refunds", "http:POST https://api.acme.example/refunds"),
        ("tool/http.POST.https://api.acme.example/refunds", "host:api.acme.example"),
        # The tool's name is split off the ref, so its server matches an ADK toolset for the same URL.
        ("tool/mcp.https://mcp.acme.example/mcp/.search_orders", "mcp:https://mcp.acme.example/mcp"),
        ("tool/mcp.https://mcp.acme.example/mcp/.search_orders", "host:mcp.acme.example"),
    }


@pytest.fixture
def db(ledger_location):
    with Store(ledger_location) as store:
        store.record([SPEC, COPILOT, ADK, ADL, HR], event=SHIPPED, environment="prod")
    return ledger_location


def test_related_refs_on_other_platforms_are_found_strongest_first(db):
    with Store(db) as store:
        exact = store.dependents("tool/http.POST.https://api.acme.example/refunds")
        related = store.related("tool/http.POST.https://api.acme.example/refunds", exact=exact)
    assert [d.agent for d in exact] == ["refunds-spec"]
    assert [(d.agent, d.match, d.via) for d in related] == [
        ("refunds-copilot", "same HTTP operation", "http:POST https://api.acme.example/refunds"),
        ("ledger-adl", "same API host", "host:api.acme.example"),
    ]


def test_mcp_servers_and_knowledge_urls_match_across_formats(db):
    with Store(db) as store:
        [orders] = store.related("tool/mcp.https://mcp.acme.example/mcp")
        [hr] = store.related("knowledge/https://contoso.sharepoint.com/sites/hr")
    assert (orders.agent, orders.ref, orders.match) == (
        "refunds-spec", "tool/mcp.https://mcp.acme.example/mcp/.search_orders", "same MCP server")
    assert (hr.agent, hr.match) == ("refunds-copilot", "same knowledge source")


def test_a_target_can_be_searched_directly(db):
    with Store(db) as store:
        found = store.related("host:api.acme.example")
    assert {d.agent for d in found} == {"refunds-spec", "refunds-copilot", "ledger-adl"}
    assert {d.match for d in found} == {"same API host"}


def test_deps_lists_related_refs_unless_exact(db, capsys):
    main(["deps", "tool/http.POST.https://api.acme.example/refunds", "--db", db])
    out = capsys.readouterr().out
    assert "Reaching the same thing, on any platform:" in out
    assert "refunds-copilot" in out and "same HTTP operation" in out
    main(["deps", "tool/http.POST.https://api.acme.example/refunds", "--db", db, "--exact"])
    assert "refunds-copilot" not in capsys.readouterr().out
    main(["deps", "host:api.acme.example", "--db", db, "--output", "json"])
    assert {d["agent"] for d in json.loads(capsys.readouterr().out)} == {"refunds-spec", "refunds-copilot", "ledger-adl"}


def test_web_view_and_api_show_related_refs(db):
    client = TestClient(create_web_app(db, WebSettings(enabled=True, allow_unauthenticated=True)))
    page = client.get("/deps", params={"q": "tool/http.POST.https://api.acme.example/refunds"}).text
    assert "Reaching the same thing, on any platform" in page and "refunds-copilot" in page
    api = client.get("/v0/deps", params={"q": "tool/http.POST.https://api.acme.example/refunds"}).json()
    assert [(d["agent"], d["match"]) for d in api] == [
        ("refunds-spec", "exact"), ("refunds-copilot", "same HTTP operation"), ("ledger-adl", "same API host")]
    exact = client.get("/v0/deps", params={"q": "tool/http.POST.https://api.acme.example/refunds", "resolve": "false"}).json()
    assert [d["agent"] for d in exact] == ["refunds-spec"]


def bedrock_stack(stack, executor, function_name=None):
    """A template with one agent whose action group runs on ``executor``."""
    from marshal_ledger.readers.cloudformation import CloudFormationReader, load_template

    function = ""
    if function_name is not None:  # "" for a function whose name CloudFormation generates
        name = f"FunctionName: {function_name}, " if function_name else ""
        function = f"""
          Fn:
            Type: AWS::Lambda::Function
            Properties: {{{name}Runtime: python3.12, Handler: app.handler, Role: arn:aws:iam::123456789012:role/fn}}"""
    template = f"""
        Resources:{function}
          Agent:
            Type: AWS::Bedrock::Agent
            Properties:
              AgentName: {stack}
              Instruction: Look up refunds and issue them within the policy limit.
              ActionGroups:
                - ActionGroupName: refunds
                  ActionGroupExecutor: {{Lambda: {executor}}}
                  FunctionSchema: {{Functions: [{{Name: issue_refund}}]}}
    """
    [record] = CloudFormationReader().read_data(load_template(textwrap.dedent(template)), f"{stack}.yaml").records
    return record


@pytest.mark.parametrize("executor, function_name, target", [
    ("!ImportValue refunds-stack-FunctionArn", None, "lambda:import:refunds-stack-FunctionArn"),
    ("arn:aws:lambda:us-east-1:123456789012:function:refunds:live", None,
     "lambda:arn:aws:lambda:us-east-1:123456789012:function:refunds"),
    ("!GetAtt Fn.Arn", "refunds-handler", "lambda:function:refunds-handler"),
    ("!GetAtt Fn.Arn", "!Sub '${AWS::StackName}-refunds'", None),   # a name only this stack knows
    ("!GetAtt Fn.Arn", "", None),
])
def test_bedrock_executors_resolve_to_their_lambda(executor, function_name, target):
    record = bedrock_stack("refunds", executor, function_name)
    found = [t.target for t in targets(record) if t.kind == "lambda"]
    assert found == ([target] if target else [])
    assert not record.spec.extensions["bedrock"].get("executors"), "kept out of the hashed spec"


def test_agents_in_different_stacks_sharing_an_exported_lambda(ledger_location):
    with Store(ledger_location) as store:
        store.record([bedrock_stack("support", "!ImportValue refunds-stack-FunctionArn"),
                      bedrock_stack("billing", "!ImportValue refunds-stack-FunctionArn")], event=SHIPPED, environment="prod")
        [other] = store.related("tool/bedrock.support.*")
    assert (other.agent, other.match, other.via) == ("billing/Agent", "same Lambda export", "lambda:import:refunds-stack-FunctionArn")


def test_every_documented_equivalent_cites_its_sources():
    from marshal_ledger.resolve import KINDS, documented_equivalents

    table = documented_equivalents()
    assert table, "the table loads from package data"
    for ref, entries in table.items():
        assert ref.split("/", 1)[0] in ("tool", "knowledge") and "*" not in ref
        for target, sources in entries:
            assert target.split(":", 1)[0] in KINDS
            assert sources and all(s.startswith("https://") for s in sources)


LEARN_CONNECTOR = record("docs-copilot", "copilot-studio",
                         tools=[("tool/mcp.powerplatform.shared_microsoftlearndocsmcpserver", "Microsoft Learn Docs MCP Server")])
LEARN_ADK = record("docs-adk", "google-adk", tools=[("tool/mcp.https://learn.microsoft.com/api/mcp/", "McpToolset")])


def test_a_documented_connector_matches_the_server_it_calls(ledger_location):
    with Store(ledger_location) as store:
        store.record([LEARN_CONNECTOR, LEARN_ADK], event=SHIPPED, environment="prod")
        [from_connector] = store.related("*shared_microsoftlearndocsmcpserver")
        [from_server] = store.related("tool/mcp.https://learn.microsoft.com/api/mcp/")
    assert (from_connector.agent, from_connector.match) == ("docs-adk", "same MCP server (documented)")
    assert (from_server.agent, from_server.match) == ("docs-copilot", "same MCP server (documented)")
    assert "https://learn.microsoft.com/en-us/connectors/microsoftlearndocsmcpserver/" in from_server.basis


LINKS_FILE = """
marshal: agent/v0
id: billing/refund-bot
platform: in-house
attested_by: billing-platform@example.com
attested_at: 2026-10-27
tools:
  - ref: tool/in-house.refunds
    name: refunds
links:
  - ref: tool/python.billing/refunds:issue_refund
    same_as: http:post https://API.acme.example/refunds/
  - ref: tool/in-house.refunds
    same_as: tool/python.billing/refunds:issue_refund
  - ref: refunds                      # not a full ref
    same_as: host:api.acme.example
  - ref: tool/in-house.refunds
    same_as: http:FETCH https://api.acme.example/refunds   # not a target
"""


def marshal_file(tmp_path, text=LINKS_FILE):
    from marshal_ledger.readers import READERS

    path = tmp_path / "refund-bot.marshal.yaml"
    path.write_text(text)
    return READERS["marshal-file"]().read(path)


def test_marshal_file_links_are_normalized_and_checked(tmp_path):
    result = marshal_file(tmp_path)
    [record] = result.records
    assert record.spec.extensions["marshal-file"]["links"] == [
        {"ref": "tool/python.billing/refunds:issue_refund", "same_as": "http:POST https://api.acme.example/refunds"},
        {"ref": "tool/in-house.refunds", "same_as": "tool/python.billing/refunds:issue_refund"},
    ]
    assert sum("links[" in w for w in result.warnings) == 2


def test_marshal_file_links_round_trip(tmp_path):
    from marshal_ledger.convert import convert

    [record] = marshal_file(tmp_path).records
    conversion = convert(record, "marshal-file")
    assert conversion.report.lossy == []
    assert conversion.read_back.version == record.version
    assert "\nlinks:\n" in conversion.text


def test_attested_links_join_refs_no_definition_connects(tmp_path, ledger_location):
    [linker] = marshal_file(tmp_path).records
    python_agent = record("billing/refunds", "python", tools=[("tool/python.billing/refunds:issue_refund", "issue_refund")])
    with Store(ledger_location) as store:
        store.record([SPEC, python_agent, linker], event=SHIPPED, environment="prod")
        found = {d.agent: d for d in store.related("tool/http.POST.https://api.acme.example/refunds")}
        linked = {d.agent: d.match for d in store.related("tool/python.billing/refunds:issue_refund")}
    python = found["billing/refunds"]
    assert python.match == "same HTTP operation (attested)"
    assert "attested by billing-platform@example.com on 2026-10-27" in python.basis
    # From the Python tool: the Agent Spec tool through the HTTP link, and the hand-written
    # agent's own tool through the ref-to-ref link.
    assert linked == {"refunds-spec": "same HTTP operation (attested)", "billing/refund-bot": "same entity (attested)"}
