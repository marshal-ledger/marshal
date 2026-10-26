from datetime import datetime, timedelta, timezone

import pytest

from marshal_ledger.model import AgentSpec, DeploymentEvent, Entity, EntityKind, Source, ToolUse, Value
from marshal_ledger.readers.base import build_record
from marshal_ledger.store import SCHEMA_VERSION, Store, StoreError

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
SHIPPED, PROPOSED = DeploymentEvent.SHIPPED, DeploymentEvent.PROPOSED


def record(agent="finance/invoice", description="Reconciles invoices", tools=(), commit=None, bindings=None):
    spec = AgentSpec(
        description=Value[str].declared(description),
        tools=[ToolUse(ref=ref) for ref in tools],
    )
    source = Source(format="test", artifact="agent.yml", commit=commit)
    return build_record(agent=agent, platform="test", source=source, spec=spec, bindings=bindings)


@pytest.fixture
def store(ledger_location):
    with Store(ledger_location) as s:
        yield s


def test_versions_get_sequence_numbers_in_first_seen_order(store):
    [first] = store.record([record()], event=SHIPPED, environment="prod", at=T0)
    [again] = store.record([record()], event=SHIPPED, environment="dev", at=T0 + timedelta(1))
    [second] = store.record([record(description="v2")], event=PROPOSED, environment="prod", at=T0 + timedelta(2))
    assert (first.version.label, first.new_version) == ("v1", True)
    assert (again.version.label, again.new_version) == ("v1", False)
    assert (second.version.label, second.new_version) == ("v2", True)


def test_resolve_selectors(store):
    v1 = record()
    v2 = record(description="v2")
    store.record([v1], event=SHIPPED, environment="prod", at=T0)
    store.record([v2], event=SHIPPED, environment="dev", at=T0 + timedelta(1))
    store.record([v2], event=PROPOSED, environment="prod", at=T0 + timedelta(2))

    agent = "finance/invoice"
    assert store.resolve(agent, "v1").record.version == v1.version
    assert store.resolve(agent, "latest").label == "v2"
    assert store.resolve(agent, "shipped").label == "v2"
    assert store.resolve(agent, "shipped:prod").label == "v1"
    assert store.resolve(agent, "proposed:prod").label == "v2"
    assert store.resolve(agent, v1.version[7:19]).label == "v1"
    assert store.resolve(agent, v1.version).label == "v1"
    with pytest.raises(StoreError):
        store.resolve(agent, "shipped:staging")
    with pytest.raises(StoreError):
        store.resolve(agent, "v9")
    with pytest.raises(StoreError, match="unknown agent"):
        store.resolve("nobody", "latest")


def test_rollback_makes_older_version_latest(store):
    store.record([record()], event=SHIPPED, environment="prod", at=T0)
    store.record([record(description="v2")], event=SHIPPED, environment="prod", at=T0 + timedelta(1))
    store.record([record()], event=SHIPPED, environment="prod", at=T0 + timedelta(2))
    assert store.resolve("finance/invoice", "shipped:prod").label == "v1"
    assert store.agents()[0].shipped == {"prod": 1}


def test_bindings_stay_on_events_not_versions(store):
    store.record([record(bindings={"target": "prod"})], event=SHIPPED, environment="prod", at=T0)
    assert store.resolve("finance/invoice", "v1").record.bindings == {}


def test_history_is_an_audit_trail(store):
    store.record([record(commit="abc")], event=PROPOSED, environment="prod", at=T0)
    store.record([record(commit="abc")], event=SHIPPED, environment="prod", at=T0 + timedelta(1))
    history = store.history("finance/invoice")
    assert [(e.event, e.environment, e.commit, seq) for e, seq, _ in history] == [
        (PROPOSED, "prod", "abc", 1),
        (SHIPPED, "prod", "abc", 1),
    ]


def test_list_filters_by_tool(store):
    entity = Entity(
        ref="tool/sap.update_po", kind=EntityKind.TOOL, platform="test", identifier="sap.update_po"
    )
    store.record(
        [record(tools=["tool/sap.update_po"]), record(agent="hr/onboarding", tools=["tool/workday.read"])],
        entities=[entity],
        event=SHIPPED,
        environment="prod",
        at=T0,
    )
    assert [a.id for a in store.agents()] == ["finance/invoice", "hr/onboarding"]
    assert [a.id for a in store.agents(tool="sap.*")] == ["finance/invoice"]
    assert [a.id for a in store.agents(tool="tool/workday.*")] == ["hr/onboarding"]
    assert store.agents(tool="salesforce.*") == []


def test_store_persists_and_checks_schema_version(tmp_path):
    path = tmp_path / "ledger.sqlite"
    with Store(path) as s:
        s.record([record()], event=SHIPPED, environment="prod", at=T0)
    with Store(path) as s:
        assert s.resolve("finance/invoice", "latest").label == "v1"
        s._db.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION + 1),))
        s._db.commit()
    with pytest.raises(StoreError, match="schema version"):
        Store(path)


def test_version_1_store_is_migrated(tmp_path):
    import sqlite3

    from marshal_ledger import store as store_module

    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.executescript(store_module._SCHEMA_V1)
    db.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '1')")
    db.commit()
    db.close()
    with Store(path) as s:
        version = s._db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
        assert version == str(SCHEMA_VERSION)
        s.record([record()], event=SHIPPED, environment="prod", at=T0)


def test_ownership(store):
    from marshal_ledger.store import Actor, OwnershipError

    team = Actor(issuer="gh", repository="acme/finance")
    other = Actor(issuer="gh", repository="acme/other")
    store.record([record()], event=SHIPPED, environment="prod", at=T0, actor=team)
    assert store.owner("finance/invoice") == ("gh", "acme/finance")
    with pytest.raises(OwnershipError):
        store.record([record(), record(agent="other/agent")], event=SHIPPED, environment="prod", actor=other)
    assert not store.has_agent("other/agent"), "a refused batch writes nothing"
    store.transfer("finance/invoice", "gh", "acme/other")
    store.record([record()], event=SHIPPED, environment="prod", actor=other)


def test_dependents_and_entities(store):
    from marshal_ledger.model import Entity, EntityKind

    po = Entity(ref="tool/sap.update_po", kind=EntityKind.TOOL, platform="test", identifier="sap.update_po")
    store.record([record(tools=["tool/sap.update_po"])], entities=[po], event=SHIPPED, environment="prod", at=T0)
    store.record([record(tools=["tool/sap.read_po"])], event=PROPOSED, environment="prod", at=T0 + timedelta(1))
    store.record([record(agent="hr/onboarding", tools=["tool/workday.read"])], event=SHIPPED, environment="prod", at=T0)

    # The shipped version still uses the tool even though the latest proposal doesn't.
    [dependent] = store.dependents("sap.update_po")
    assert (dependent.agent, dependent.label, dependent.shipped, dependent.latest) == ("finance/invoice", "v1", ("prod",), False)
    assert {d.ref for d in store.dependents("tool/sap.*")} == {"tool/sap.update_po", "tool/sap.read_po"}
    assert store.dependents("tool/sap.read_po", shipped_only=True) == []
    counts = {e.ref: n for e, n in store.entities()}
    assert counts == {"tool/sap.update_po": 0}, "only stored entities are listed; counts follow latest versions"


def test_concurrent_writers_get_distinct_sequence_numbers(ledger_location):
    if not ledger_location.startswith("postgresql://"):
        pytest.skip("concurrency is exercised against Postgres (set MARSHAL_TEST_POSTGRES_URL)")
    import threading

    with Store(ledger_location) as s:
        s.record([record()], event=SHIPPED, environment="prod", at=T0)
    errors = []

    def write(n):
        try:
            with Store(ledger_location) as s:
                s.record([record(description=f"variant {n}")], event=PROPOSED, environment="prod")
        except Exception as exc:  # surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with Store(ledger_location) as s:
        labels = sorted(int(seq) for _, seq, _ in s.history("finance/invoice"))
    assert labels == list(range(1, 10))
