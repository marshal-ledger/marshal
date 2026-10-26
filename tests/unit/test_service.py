from datetime import datetime, timezone

import pytest

from marshal_ledger.hashing import sha256_text
from marshal_ledger.model import AgentSpec, DeploymentEvent, Instructions, Provenance, Source, ToolUse, Value
from marshal_ledger.readers.base import build_record
from marshal_ledger.report import COMMENT_MARKER, markdown, text
from marshal_ledger.service import AgentReport, ingest, normalize
from marshal_ledger.store import Store

FAKE_TOKEN = "dapi" + "0123456789abcdef" * 2


def record(instructions="Reconcile invoices.", tools=()):
    spec = AgentSpec(
        instructions=Instructions(hash=sha256_text(instructions), text=instructions, provenance=Provenance.DECLARED),
        tools=[ToolUse(ref=t) for t in tools],
    )
    return build_record(
        agent="finance/invoice", platform="test", source=Source(format="test", artifact="a.yml"), spec=spec
    )


@pytest.fixture
def store(ledger_location):
    with Store(ledger_location) as s:
        yield s


def test_normalize_recomputes_version_and_ignores_client_claims():
    original = record()
    tampered = original.model_copy(update={"version": "sha256:" + "0" * 64}, deep=True)
    tampered.quality.completeness = 1.0
    normalized, _ = normalize(tampered)
    assert normalized.version == original.version
    assert normalized.quality == original.quality


def test_normalize_redacts_and_rehashes():
    normalized, count = normalize(record(f"Use {FAKE_TOKEN} to call the API."))
    assert count == 1
    assert FAKE_TOKEN not in normalized.spec.instructions.text
    assert normalized.spec.instructions.hash == sha256_text(normalized.spec.instructions.text)


def test_hash_only_mode_drops_text_but_keeps_version(store):
    full, _ = normalize(record())
    hashed, _ = normalize(record(), hash_only=True)
    assert hashed.spec.instructions.text is None
    assert hashed.version == full.version


def test_ingest_reports_against_last_shipped(store):
    [first] = ingest(store, [record()], event=DeploymentEvent.SHIPPED, environment="prod")
    assert first.baseline is None
    assert first.label == "v1"

    [proposed] = ingest(
        store, [record(tools=["tool/sap.update_po"])], event=DeploymentEvent.PROPOSED, environment="prod"
    )
    assert (proposed.baseline, proposed.label) == ("v1", "v2")
    assert [c.change.path for c in proposed.changes] == ["tool tool/sap.update_po"]
    assert proposed.changes[0].severity.label == "HIGH"

    [shipped] = ingest(
        store, [record(tools=["tool/sap.update_po"])], event=DeploymentEvent.SHIPPED, environment="prod"
    )
    assert shipped.baseline == "v1", "a shipped event compares against the version shipped before it"
    [again] = ingest(store, [record(tools=["tool/sap.update_po"])], event=DeploymentEvent.PROPOSED, environment="prod")
    assert again.baseline == "v2" and again.changes == []


def test_reports_round_trip_and_render(store):
    ingest(store, [record()], event=DeploymentEvent.SHIPPED, environment="prod")
    reports = ingest(store, [record(tools=["tool/x"])], event=DeploymentEvent.PROPOSED, environment="prod")
    restored = [AgentReport.from_dict(r.to_dict()) for r in reports]
    assert restored == reports

    comment = markdown(restored)
    assert comment.startswith(COMMENT_MARKER)
    assert "### Marshal: agent changes for `prod`" in comment
    assert "**finance/invoice** (test) · v1 → v2 · 1 change, 1 high risk" in comment
    assert "HIGH  + tool tool/x (scope unknown, reversible unknown)" in comment
    assert "Completeness" in text(restored)


def test_markdown_lists_unchanged_agents(store):
    ingest(store, [record()], event=DeploymentEvent.SHIPPED, environment="prod")
    reports = ingest(store, [record()], event=DeploymentEvent.PROPOSED, environment="prod")
    assert "Unchanged from what is shipped: `finance/invoice`" in markdown(reports)
