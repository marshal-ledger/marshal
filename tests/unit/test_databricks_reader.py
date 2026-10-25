import json
import textwrap

import pytest

from marshal_ledger.model import IdentityKind, Provenance, Scope
from marshal_ledger.readers import ReadOptions, detect
from marshal_ledger.readers.databricks import BundleError, DatabricksBundleReader


# Built by concatenation so the source never contains a credential-shaped literal.
FAKE_TOKEN = "dapi" + "0123456789abcdef" * 2


def write(root, files):
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content) if isinstance(content, str) else json.dumps(content))
    return root


def read(root, **options):
    return DatabricksBundleReader().read(root, ReadOptions(**options))


AGENT_APP = f"""
    bundle:
      name: support
    resources:
      apps:
        support_agent:
          name: support-agent
          description: Answers support questions
          source_code_path: ./agent
          config:
            command: ["uv", "run", "start-app"]
            env:
              - name: API_TOKEN
                value: "{FAKE_TOKEN}"
              - name: MLFLOW_EXPERIMENT_ID
                value_from: experiment
          resources:
            - name: llm
              serving_endpoint: {{name: databricks-claude-sonnet-5-5, permission: CAN_QUERY}}
            - name: lookup
              uc_securable: {{securable_full_name: main.support.lookup_order, securable_type: FUNCTION, permission: EXECUTE}}
            - name: faq
              uc_securable: {{securable_full_name: main.support.faq, securable_type: TABLE, permission: SELECT}}
            - name: experiment
              experiment: {{experiment_id: "", permission: CAN_MANAGE}}
        dashboard:
          name: plain-dashboard
          resources:
            - name: wh
              sql_warehouse: {{id: abc, permission: CAN_USE}}
      model_serving_endpoints:
        classifier:
          name: churn-classifier
    targets:
      dev:
        default: true
        mode: development
      prod:
        mode: production
        workspace: {{host: https://prod.example.cloud.databricks.com}}
        resources:
          apps:
            support_agent:
              name: support-agent-prod
"""


def test_detects_bundle(tmp_path):
    write(tmp_path, {"databricks.yml": AGENT_APP})
    assert detect(tmp_path) is DatabricksBundleReader
    assert detect(tmp_path / "databricks.yml") is DatabricksBundleReader
    assert not DatabricksBundleReader.detect(tmp_path / "nothing")


def test_app_with_model_access_is_an_agent(tmp_path):
    result = read(write(tmp_path, {"databricks.yml": AGENT_APP}))
    assert [r.agent for r in result.records] == ["support/support_agent"]
    record = result.records[0]
    spec = record.spec

    assert spec.display_name.value == "support-agent"
    assert spec.model.requested.provenance is Provenance.UNKNOWN
    assert spec.permissions.identity.value is IdentityKind.SERVICE_PRINCIPAL
    assert [t.ref for t in spec.tools] == ["tool/databricks.uc.function.main.support.lookup_order"]
    assert spec.tools[0].scope.value is Scope.EXECUTE
    assert spec.tools[0].reversible.provenance is Provenance.UNKNOWN
    assert [k.ref for k in spec.knowledge] == ["knowledge/databricks.uc.table.main.support.faq"]
    assert spec.knowledge[0].scope.value is Scope.READ
    grants = {g.target: g.privilege for g in spec.permissions.grants}
    assert grants["model/databricks.serving_endpoint.databricks-claude-sonnet-5-5"] == "CAN_QUERY"
    assert record.source.artifact == "databricks.yml"
    assert record.source.locator == "resources.apps.support_agent"


def test_environment_values_never_reach_the_record(tmp_path):
    result = read(write(tmp_path, {"databricks.yml": AGENT_APP}))
    dumped = result.records[0].model_dump_json()
    assert FAKE_TOKEN not in dumped
    assert {"name": "API_TOKEN"} in result.records[0].spec.extensions["databricks"]["env"]


def test_non_agent_items_are_reported_as_skipped(tmp_path):
    result = read(write(tmp_path, {"databricks.yml": AGENT_APP}))
    skipped = {s.locator: s.reason for s in result.skipped}
    assert "resources.apps.dashboard" in skipped
    assert "resources.model_serving_endpoints.classifier" in skipped


def test_target_name_override_is_a_binding_not_a_new_version(tmp_path):
    write(tmp_path, {"databricks.yml": AGENT_APP})
    base = read(tmp_path).records[0]
    prod = read(tmp_path, target="prod").records[0]
    assert prod.version == base.version
    assert prod.agent == base.agent
    assert prod.bindings == {
        "target": "prod",
        "mode": "production",
        "workspace_host": "https://prod.example.cloud.databricks.com",
        "deployed_name": "support-agent-prod",
    }


def test_target_that_changes_resources_changes_version(tmp_path):
    bundle = AGENT_APP.replace(
        "              name: support-agent-prod",
        "              name: support-agent-prod\n"
        "              resources:\n"
        "                - name: llm\n"
        "                  serving_endpoint: {name: databricks-gpt-5, permission: CAN_QUERY}",
    )
    write(tmp_path, {"databricks.yml": bundle})
    prod = read(tmp_path, target="prod")
    assert prod.records[0].version != read(tmp_path).records[0].version
    assert any("lists are replaced" in w for w in prod.warnings)


def test_unknown_target_is_an_error(tmp_path):
    write(tmp_path, {"databricks.yml": AGENT_APP})
    with pytest.raises(BundleError, match="staging"):
        read(tmp_path, target="staging")


GENIE_SPACE = {
    "version": 2,
    "config": {"sample_questions": [{"id": "1", "question": ["Top vendors by spend?"]}]},
    "data_sources": {"tables": [{"identifier": "main.finance.invoices"}]},
    "instructions": {
        "text_instructions": [{"id": "2", "content": ["Amounts are in EUR.\n", "Fiscal year starts in April."]}]
    },
}


def test_genie_space_from_file(tmp_path):
    write(
        tmp_path,
        {
            "databricks.yml": """
                bundle: {name: finance}
                include: [resources/*.yml]
            """,
            "resources/spend.yml": """
                resources:
                  genie_spaces:
                    spend:
                      title: Vendor spend
                      file_path: ../src/spend.geniespace.json
                      warehouse_id: ${var.warehouse_id}
                      permissions:
                        - {group_name: finance-analysts, level: CAN_RUN}
            """,
            "src/spend.geniespace.json": GENIE_SPACE,
        },
    )
    result = read(tmp_path)
    record = result.records[0]
    assert record.agent == "finance/spend"
    assert record.source.artifact == "resources/spend.yml"
    assert record.spec.instructions.text == "Amounts are in EUR.\nFiscal year starts in April."
    assert record.spec.instructions.basis.startswith("src/spend.geniespace.json")
    assert [k.ref for k in record.spec.knowledge] == ["knowledge/databricks.uc.table.main.finance.invoices"]
    assert record.spec.permissions.identity.value is IdentityKind.ON_BEHALF_OF_USER
    assert record.spec.permissions.access[0].principal == "finance-analysts"
    assert record.bindings["warehouse_id"] == "${var.warehouse_id}"
    assert record.spec.extensions["databricks"]["sample_questions"] == ["Top vendors by spend?"]


def test_genie_space_inline_and_hash_only(tmp_path):
    write(
        tmp_path,
        {
            "databricks.yml": f"""
                bundle: {{name: finance}}
                resources:
                  genie_spaces:
                    spend:
                      title: Vendor spend
                      serialized_space: '{json.dumps(GENIE_SPACE)}'
            """
        },
    )
    full = read(tmp_path).records[0]
    hashed = read(tmp_path, hash_only=True).records[0]
    assert full.spec.instructions.text
    assert hashed.spec.instructions.text is None
    assert hashed.spec.instructions.hash == full.spec.instructions.hash
    assert hashed.version == full.version


def test_app_calling_bundle_genie_space_links_to_its_agent(tmp_path):
    write(
        tmp_path,
        {
            "databricks.yml": f"""
                bundle: {{name: finance}}
                resources:
                  genie_spaces:
                    spend:
                      title: Vendor spend
                      serialized_space: '{json.dumps(GENIE_SPACE)}'
                  apps:
                    spend_chat:
                      name: spend-chat
                      resources:
                        - name: genie
                          genie_space:
                            name: Vendor spend
                            space_id: ${{resources.genie_spaces.spend.space_id}}
                            permission: CAN_RUN
            """
        },
    )
    result = read(tmp_path)
    app = next(r for r in result.records if r.agent == "finance/spend_chat")
    assert app.spec.tools[0].ref == "agent/finance/spend"
    assert app.spec.tools[0].scope.value is Scope.READ
    assert "agent/finance/spend" in {e.ref for e in result.entities}


def test_credentials_in_instructions_are_redacted(tmp_path):
    space = json.loads(json.dumps(GENIE_SPACE))
    space["instructions"]["text_instructions"][0]["content"] = [f"Use token {FAKE_TOKEN}."]
    write(
        tmp_path,
        {
            "databricks.yml": """
                bundle: {name: finance}
                resources:
                  genie_spaces:
                    spend: {title: Spend, file_path: spend.geniespace.json}
            """,
            "spend.geniespace.json": space,
        },
    )
    result = read(tmp_path)
    assert FAKE_TOKEN not in result.records[0].spec.instructions.text
    assert any("redacted" in w for w in result.warnings)


def test_duplicate_resource_keys_warn(tmp_path):
    write(
        tmp_path,
        {
            "databricks.yml": """
                bundle: {name: dup}
                include: [more.yml]
                resources:
                  genie_spaces:
                    spend: {title: First}
            """,
            "more.yml": """
                resources:
                  genie_spaces:
                    spend: {title: Second}
            """,
        },
    )
    result = read(tmp_path)
    assert result.records[0].spec.display_name.value == "First"
    assert any("defined in both" in w for w in result.warnings)
