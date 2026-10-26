"""Marshal files: hand-written agent definitions, recorded as attested."""

from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from marshal_ledger.cli import main
from marshal_ledger.convert import convert
from marshal_ledger.model import Channel, DeploymentEvent, IdentityKind, Provenance, Scope
from marshal_ledger.readers import detect
from marshal_ledger.readers.marshalfile import MarshalFileReader
from marshal_ledger.service import normalize
from marshal_ledger.store import Store
from marshal_ledger.web import WebSettings, create_web_app
from marshal_ledger.writers.marshalfile import template

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "agents" / "refund-assistant.marshal.yaml"
FAKE_KEY = "sk-" + "a1B2c3D4e5F6g7H8i9J0" * 2

MINIMAL = {"marshal": "agent/v0", "id": "ops/triage", "platform": "acme.flow"}


def read(doc, artifact="triage.marshal.yaml"):
    return MarshalFileReader().read_data(doc, artifact)


def test_example_is_detected_and_attested():
    assert detect(EXAMPLE) is MarshalFileReader
    result = MarshalFileReader().read(EXAMPLE)
    assert result.warnings == []
    [record] = result.records
    spec = record.spec
    assert (record.agent, record.platform) == ("payments/refund-assistant", "orchestrator.internal")
    assert spec.display_name.provenance is Provenance.ATTESTED
    assert spec.display_name.basis == (
        "refund-assistant.marshal.yaml#display_name (attested by payments-platform@example.com on 2026-10-20)"
    )
    assert spec.instructions.provenance is Provenance.ATTESTED and spec.instructions.text.startswith("You help")
    tools = {t.name: t for t in spec.tools}
    assert tools["issue_refund"].scope.value is Scope.WRITE
    assert tools["issue_refund"].reversible.value is False
    assert tools["issue_refund"].requires_confirmation.provenance is Provenance.ATTESTED
    assert tools["open_escalation"].requires_confirmation.provenance is Provenance.UNKNOWN
    assert "stated as unknown" in tools["open_escalation"].requires_confirmation.basis
    assert spec.permissions.identity.value is IdentityKind.SERVICE_PRINCIPAL
    assert {g.provenance for g in spec.permissions.grants} == {Provenance.ATTESTED}
    assert [a.level for a in spec.permissions.access] == ["use", "manage"]
    assert record.bindings["env"] == ["PAYMENTS_API_URL", "PAYMENTS_API_KEY"]
    assert spec.extensions == {"orchestrator.internal": {"refund_limit_usd": 200, "queue": "support-tier-1"}}
    assert record.quality.completeness == 1.0
    assert "attested by payments-platform@example.com" in record.source.detected_by
    refs = {e.ref: e.kind.value for e in result.entities}
    assert refs["store/postgres.payments.refunds"] == "store"
    assert refs["knowledge/confluence.SUPPORT.refund-policy"] == "knowledge"


def test_omitted_fields_are_unknown_not_guessed():
    [record] = read(MINIMAL).records
    spec = record.spec
    assert record.quality.completeness == 0.0
    for value in (spec.display_name, spec.model.requested, spec.permissions.identity, spec.permissions.principal):
        assert value.provenance is Provenance.UNKNOWN
        assert value.basis == "not stated in the hand-written definition triage.marshal.yaml"
    assert spec.instructions.provenance is Provenance.UNKNOWN and spec.instructions.hash is None
    assert normalize(record)[0].spec.instructions.provenance is Provenance.UNKNOWN


def test_provenance_does_not_change_the_version():
    """The same values in an ADL file and a Marshal file are one version."""
    from marshal_ledger.readers.adl import AdlReader

    adl = {"adl_spec": "0.3.0", "name": "Triage", "description": "Sorts tickets.", "version": "1.0.0",
           "model": {"provider": "openai", "name": "gpt-5"}, "system_prompt": "Sort tickets."}
    [declared] = AdlReader().read_data(adl, "t.adl.yaml").records
    [attested] = read({**MINIMAL, "display_name": "Triage", "description": "Sorts tickets.",
                       "model": {"requested": "gpt-5", "provider": "OpenAI"}, "instructions": "Sort tickets.",
                       "extensions": {"adl": declared.spec.extensions["adl"]}}).records
    assert attested.version == declared.version


def test_credentials_are_never_copied():
    doc = {
        **MINIMAL,
        "description": f"Uses key {FAKE_KEY}",
        "instructions": f"Call the API with {FAKE_KEY}.",
        "tools": [{"ref": "tool/acme.flow.http", "description": f"Bearer {FAKE_KEY}",
                   "input_schema": {"properties": {"api_key": {"default": FAKE_KEY}}}}],
        "model": {"requested": "gpt-5", "parameters": {"api_key": FAKE_KEY}},
        "bindings": {"env": {"OPENAI_API_KEY": FAKE_KEY}, "client_secret": "hunter2", "endpoint": "https://x.example"},
        "extensions": {"acme.flow": {"auth": {"token": "plain-secret"}, "note": f"key={FAKE_KEY}"}},
        "attested_by": f"ops {FAKE_KEY}",
    }
    result = read(doc)
    [record] = result.records
    dumped = record.model_dump_json()
    for secret in (FAKE_KEY, "hunter2", "plain-secret"):
        assert secret not in dumped
    assert record.bindings == {"env": ["OPENAI_API_KEY"], "client_secret": "<redacted>", "endpoint": "https://x.example"}
    assert record.spec.extensions["acme.flow"]["auth"] == {"token": "<redacted>"}
    assert any("environment variables are recorded by name only" in w for w in result.warnings)
    assert any("credential-like" in w for w in result.warnings)


def test_invalid_values_warn_and_stay_unknown():
    doc = {
        **MINIMAL,
        "tools": ["notify", {"name": "wipe", "scope": "everything", "reversible": "no"}, {"description": "nameless"}],
        "knowledge": [{"name": "faq", "scope": "read"}],
        "permissions": {
            "identity": "robot",
            "grants": [{"target": "store/db.t", "privilege": "ALL"}, {"target": "db.u", "privilege": "SELECT", "scope": "read"}],
            "access": [{"principal": "ops", "principal_type": "team", "level": "use"}, {"principal": "x"}],
        },
        "tool": [{"name": "typo"}],
    }
    result = read(doc)
    [record] = result.records
    spec = record.spec
    assert [t.ref for t in spec.tools] == ["tool/acme.flow.notify", "tool/acme.flow.wipe"]
    assert spec.tools[1].scope.provenance is Provenance.UNKNOWN
    assert spec.tools[1].reversible.provenance is Provenance.UNKNOWN
    assert spec.knowledge[0].ref == "knowledge/acme.flow.faq"
    assert spec.permissions.identity.provenance is Provenance.UNKNOWN
    assert [g.target for g in spec.permissions.grants] == ["other/acme.flow.db.u"]
    assert [(a.principal, a.principal_type) for a in spec.permissions.access] == [("ops", "unknown")]
    assert spec.extensions["marshal-file"] == {"tool": [{"name": "typo"}]}
    text = "\n".join(result.warnings)
    for fragment in ("'everything' is not a valid value", "'no' is not a valid value", "needs a ref or a name",
                     "'robot'", "a grant needs", "'team'", "access rule needs", "unexpected top-level key(s) tool"):
        assert fragment in text


def test_wrong_shapes_warn_rather_than_fail():
    result = read({**MINIMAL, "model": ["gpt-5"], "permissions": ["admin"], "tools": {"name": "x"},
                   "bindings": "prod", "extensions": ["x"]})
    [record] = result.records
    assert record.spec.tools == [] and record.bindings == {} and record.spec.extensions == {}
    assert len(result.warnings) == 5


def test_missing_id_or_platform_is_skipped():
    result = read({"marshal": "agent/v0", "display_name": "Nameless"})
    assert result.records == []
    assert result.skipped[0].reason == "a Marshal file needs id and platform"


def test_hash_only_instructions():
    digest = "sha256:" + "ab" * 32
    [record] = read({**MINIMAL, "instructions": {"hash": digest}}).records
    assert record.spec.instructions.hash == digest and record.spec.instructions.text is None
    assert record.spec.instructions.provenance is Provenance.ATTESTED


def test_conversion_writes_unknowns_for_a_person_to_check():
    [record] = read({**MINIMAL, "tools": [{"ref": "tool/acme.flow.notify", "scope": "write"}]}).records
    conversion = convert(record, "marshal-file")
    assert conversion.report.lossy == []
    assert conversion.text.startswith("# Marshal file")
    document = yaml.safe_load(conversion.text)
    assert document["tools"][0] == {"ref": "tool/acme.flow.notify", "scope": "write", "reversible": "unknown",
                                    "requires_confirmation": "unknown"}
    assert document["permissions"] == {"identity": "unknown", "principal": "unknown"}


def test_conversion_notes_values_it_would_not_copy():
    from marshal_ledger.readers.adl import AdlReader

    adl = {"adl_spec": "0.3.0", "name": "Caller", "description": "d", "version": "1.0.0",
           "security": {"authentication": {"api_key": "inline-value"}}}
    [record] = AdlReader().read_data(adl, "c.adl.yaml").records
    conversion = convert(record, "marshal-file")
    assert [f.path for f in conversion.report.lossy] == ["extensions.adl.security"]
    assert conversion.report.lossy[0].note == "credential-like values are not copied into a Marshal file"
    assert "inline-value" not in conversion.read_back.model_dump_json()


def test_init_agent_template_reads_back(tmp_path, capsys):
    path = tmp_path / "triage.marshal.yaml"
    assert main(["init-agent", str(path), "--id", "ops/triage", "--platform", "acme.flow"]) == 0
    result = MarshalFileReader().read(path)
    assert result.warnings == [] and result.skipped == []
    [record] = result.records
    assert (record.agent, record.platform) == ("ops/triage", "acme.flow")
    assert record.quality.completeness == 0.0
    assert record.source.detected_by == "hand-written definition (marshal: agent/v0)"
    assert main(["init-agent", str(path)]) == 1
    assert "exists" in capsys.readouterr().err
    assert template("a", "b").count("unknown") > 5


def test_web_view_marks_attested_values(ledger_location):
    [record] = MarshalFileReader().read(EXAMPLE).records
    with Store(ledger_location) as store:
        store.record([record], event=DeploymentEvent.OBSERVED, environment="prod", channel=Channel.MANUAL)
    client = TestClient(create_web_app(str(ledger_location), WebSettings(enabled=True, allow_unauthenticated=True)))
    page = client.get("/agent", params={"id": "payments/refund-assistant"}).text
    assert "hand-written" in page and 'class="attested"' in page
    assert "dashed values are attested by a person" in page
