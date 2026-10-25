"""Readers and writers for ADL, Agent Spec and OASF, on small inline documents."""

from marshal_ledger.convert import convert
from marshal_ledger.model import IdentityKind, Provenance, Scope
from marshal_ledger.readers.adl import AdlReader
from marshal_ledger.readers.agentspec import AgentSpecReader, properties_to_schema, schema_to_properties
from marshal_ledger.readers.oasf import OasfReader

FAKE_KEY = "sk-" + "a1B2c3D4e5F6g7H8i9J0" * 2

ADL = {
    "adl_spec": "0.3.0",
    "name": "Invoice Processor",
    "description": "Matches invoices to purchase orders.",
    "version": "2.0.0",
    "id": "https://agents.acme.example/invoice-processor",
    "data_classification": {"sensitivity": "confidential"},
    "cryptographic_identity": {"did": "did:web:agents.acme.example:invoice-processor"},
    "model": {"provider": "anthropic", "name": "claude-opus", "temperature": 0.1, "context_window": 200000},
    "system_prompt": "Never approve payment without AP approval.",
    "tools": [
        {"name": "search_invoices", "description": "Search invoices.", "read_only": True},
        {"name": "approve_payment", "description": "Approve a payment.", "read_only": False, "requires_confirmation": True},
        {"name": "lookup_po", "description": "Look up a PO.", "annotations": {"openapi_ref": "erp.yaml", "operation_id": "getPO"}},
    ],
    "resources": [{"name": "Vendor FAQ", "type": "vector_store", "uri": "https://kb.acme.example/vendor-faq"}],
    "permissions": {
        "network": {"allowed_hosts": ["api.acme.example"]},
        "filesystem": {"allowed_paths": [{"path": "/data/invoices/**", "access": "read"}]},
        "resource_limits": {"max_duration_sec": 300},
    },
    "regulatory_scope": {"frameworks": ["SOX"]},
}


def test_adl_maps_core_fields_and_keeps_the_rest():
    [record] = AdlReader().read_data(ADL, "invoice.adl.yaml").records
    spec = record.spec
    assert record.agent == "https://agents.acme.example/invoice-processor"
    assert (spec.model.requested.value, spec.model.provider.value) == ("claude-opus", "anthropic")
    assert spec.model.parameters == {"temperature": 0.1}
    assert spec.extensions["adl"]["model"] == {"context_window": 200000}
    tools = {t.name: t for t in spec.tools}
    assert tools["search_invoices"].scope.value is Scope.READ
    assert tools["search_invoices"].scope.provenance is Provenance.DECLARED
    assert tools["search_invoices"].reversible.value is True
    assert tools["approve_payment"].scope.value is Scope.WRITE
    assert tools["approve_payment"].requires_confirmation.value is True
    assert tools["lookup_po"].ref == "tool/openapi.erp.yaml#getPO"
    assert tools["lookup_po"].scope.provenance is Provenance.UNKNOWN
    assert spec.knowledge[0].ref == "knowledge/https://kb.acme.example/vendor-faq"
    assert spec.permissions.identity.value is IdentityKind.SERVICE_PRINCIPAL
    assert spec.permissions.principal.value.startswith("did:web:")
    grants = {g.target: g.scope for g in spec.permissions.grants}
    assert grants == {"store/file./data/invoices/**": Scope.READ, "other/network.api.acme.example": Scope.EXECUTE}
    assert spec.extensions["adl"]["regulatory_scope"] == {"frameworks": ["SOX"]}


def test_adl_round_trip_is_lossless_and_cross_format_keeps_identity():
    [record] = AdlReader().read_data(ADL, "invoice.adl.yaml").records
    assert convert(record, "adl").report.lossy == []
    spec_doc = convert(record, "agent-spec")
    assert spec_doc.read_back.agent == record.agent
    assert spec_doc.read_back.platform == "adl"
    back = convert(spec_doc.read_back, "adl")
    assert back.read_back.agent == record.agent


AGENT_SPEC = {
    "component_type": "Flow",
    "name": "Support flow",
    "agentspec_version": "26.3.1",
    "nodes": [{"component_type": "AgentNode", "name": "n", "agent": {"$component_ref": "agent-1"}}],
    "$referenced_components": {
        "agent-1": {
            "component_type": "Agent",
            "id": "agent-1",
            "name": "Support Agent",
            "system_prompt": "Help customers.",
            "llm_config": {"$component_ref": "llm-1"},
            "tools": [
                {
                    "component_type": "RemoteTool",
                    "name": "refund",
                    "http_method": "POST",
                    "url": "https://api.acme.example/refunds",
                    "sensitive_headers": {"Authorization": "Bearer secret"},
                },
                {"component_type": "ServerTool", "name": "lookup", "inputs": [{"title": "order_id", "type": "string"}]},
            ],
            "toolboxes": [
                {
                    "component_type": "MCPToolBox",
                    "name": "github",
                    "client_transport": {"component_type": "StreamableHTTPTransport", "name": "t",
                                         "url": "https://mcp.example/github", "headers": {"X-Token": "secret"}},
                    "tool_filter": ["list_prs"],
                }
            ],
        },
        "llm-1": {"component_type": "OpenAiConfig", "id": "llm-1", "name": "gpt", "model_id": "gpt-5", "api_key": FAKE_KEY},
    },
}


def test_agent_spec_resolves_references_and_never_copies_credentials():
    result = AgentSpecReader().read_data(AGENT_SPEC, "support.agentspec.yaml")
    [record] = result.records
    assert record.agent == "support/support-agent"
    assert record.spec.model.requested.value == "gpt-5"
    assert record.spec.model.provider.value == "openai"
    assert record.spec.model.provider.provenance is Provenance.DERIVED
    refs = {t.ref: t for t in record.spec.tools}
    refund = refs["tool/http.POST.https://api.acme.example/refunds"]
    assert refund.scope.value is Scope.WRITE
    assert refs["tool/agentspec.support-agent.lookup"].input_schema == {
        "type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]
    }
    assert "tool/mcp.https://mcp.example/github.list_prs" in refs
    dumped = record.model_dump_json()
    assert FAKE_KEY not in dumped and "Bearer secret" not in dumped and "X-Token" not in dumped
    assert any("credentials were not recorded" in w for w in result.warnings)
    assert [s.locator for s in result.skipped] == ["Flow Support flow"]


def test_agent_spec_properties_round_trip():
    props = [{"title": "q", "type": "string"}, {"title": "limit", "type": "integer", "default": 5}]
    schema = properties_to_schema(props)
    assert schema["required"] == ["q"]
    assert schema_to_properties(schema) == props


OASF = {
    "name": "code_review_agent",
    "schema_version": "1.1.0",
    "version": "1.0.0",
    "description": "Reviews code.",
    "authors": ["Example"],
    "created_at": "2025-01-01T00:00:00Z",
    "skills": [{"name": "language_processing/language_understanding/contextual_comprehension", "id": 10101}],
    "modules": [
        {"id": 103, "name": "core/language_model", "data": {"models": [{"model": "gpt-5", "provider": "OpenAI", "api_base": "https://api.openai.com"}]}},
        {
            "id": 202,
            "name": "integration/mcp",
            "data": {
                "name": "review-server",
                "tools": [{"name": "review_diff", "description": "Review a diff"}],
                "connections": [{"type": "stdio", "command": "docker",
                                 "env_vars": [{"name": "API_KEY", "default_value": FAKE_KEY, "description": "key"}]}],
            },
        },
    ],
}


def test_oasf_maps_models_and_mcp_tools_without_secrets():
    [record] = OasfReader().read_data(OASF, "record.json").records
    assert (record.spec.model.requested.value, record.spec.model.provider.value) == ("gpt-5", "openai")
    assert record.bindings == {"language_model.api_base": "https://api.openai.com"}
    assert [t.ref for t in record.spec.tools] == ["tool/mcp.review-server.review_diff"]
    assert FAKE_KEY not in record.model_dump_json()
    assert convert(record, "oasf").report.lossy == []


def test_conversion_reports_required_values_the_source_lacks():
    [record] = AdlReader().read_data(ADL, "invoice.adl.yaml").records
    conversion = convert(record, "oasf")
    assert any("at least one author" in w for w in conversion.warnings)
    assert any("at least one skill" in w for w in conversion.warnings)
