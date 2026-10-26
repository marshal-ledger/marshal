from datetime import datetime, timedelta, timezone

import pytest

from marshal_ledger.freshness import FreshnessPolicy
from marshal_ledger.model import AgentSpec, Channel, DeploymentEvent, Source, Value
from marshal_ledger.readers.base import build_record
from marshal_ledger.service import ingest
from marshal_ledger.store import Store

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
SHIPPED, OBSERVED = DeploymentEvent.SHIPPED, DeploymentEvent.OBSERVED


def record(description="Reconciles invoices"):
    spec = AgentSpec(description=Value[str].declared(description))
    return build_record(agent="finance/invoice", platform="test", source=Source(format="test", artifact="a"), spec=spec)


@pytest.fixture
def store(ledger_location):
    with Store(ledger_location) as s:
        yield s


def status(store, days_later):
    [env] = store.status("finance/invoice", now=T0 + timedelta(days=days_later))
    return env


def test_observation_matching_shipped_is_not_drift(store):
    store.record([record()], event=SHIPPED, environment="prod", at=T0, channel=Channel.CI)
    store.record([record()], event=OBSERVED, environment="prod", at=T0 + timedelta(1), channel=Channel.SCHEDULED)
    env = status(store, 1)
    assert (env.shipped, env.observed, env.drift, env.channel) == (1, 1, False, Channel.SCHEDULED)


def test_observation_that_differs_is_drift_until_the_next_ship(store):
    store.record([record()], event=SHIPPED, environment="prod", at=T0, channel=Channel.CI)
    store.record([record("edited in the UI")], event=OBSERVED, environment="prod", at=T0 + timedelta(1), channel=Channel.SCHEDULED)
    assert status(store, 1).drift
    # Shipping again supersedes the observation until the next one.
    store.record([record()], event=SHIPPED, environment="prod", at=T0 + timedelta(2), channel=Channel.CI)
    assert not status(store, 2).drift


def test_staleness_depends_on_channel(store):
    store.record([record()], event=SHIPPED, environment="prod", at=T0, channel=Channel.MANUAL)
    assert not status(store, 29).stale
    assert status(store, 31).stale
    store.record([record()], event=OBSERVED, environment="prod", at=T0 + timedelta(31), channel=Channel.SCHEDULED)
    assert not status(store, 37).stale
    assert status(store, 39).stale


def test_ci_events_do_not_go_stale_by_age(store):
    store.record([record()], event=SHIPPED, environment="prod", at=T0, channel=Channel.CI)
    assert not status(store, 400).stale
    [env] = store.status("finance/invoice", policy=FreshnessPolicy(ci_days=90), now=T0 + timedelta(400))
    assert env.stale


def test_ingest_reports_drift(store):
    ingest(store, [record()], event=SHIPPED, environment="prod", channel=Channel.CI)
    [report] = ingest(store, [record("edited")], event=OBSERVED, environment="prod", channel=Channel.SCHEDULED)
    assert report.drift and report.baseline == "v1" and report.channel == "scheduled"
    [same] = ingest(store, [record()], event=OBSERVED, environment="prod", channel=Channel.SCHEDULED)
    assert not same.drift


def test_version_2_store_migrates_and_marks_authenticated_events_as_ci(tmp_path):
    import sqlite3

    from marshal_ledger import store as module

    path = tmp_path / "v2.sqlite"
    db = sqlite3.connect(path)
    db.executescript(module._SCHEMA_V1)
    db.executescript(module._MIGRATIONS[1])
    db.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '2')")
    db.execute("INSERT INTO agents VALUES ('a', 'p', NULL, 't', 't')")
    db.execute("INSERT INTO versions VALUES ('a', 'sha256:x', 1, ?, 't')", (record().model_dump_json(by_alias=True),))
    db.execute("INSERT INTO events (agent, version, platform, environment, event, at, bindings, actor_repository) "
               "VALUES ('a', 'sha256:x', 'p', 'prod', 'shipped', ?, '{}', 'acme/repo')", (T0.isoformat(),))
    db.execute("INSERT INTO events (agent, version, platform, environment, event, at, bindings) "
               "VALUES ('a', 'sha256:x', 'p', 'dev', 'shipped', ?, '{}')", (T0.isoformat(),))
    db.commit()
    db.close()
    with Store(path) as s:
        channels = {e.environment: e.channel for e, _, _ in s.history("a")}
        assert channels == {"prod": Channel.CI, "dev": Channel.MANUAL}
        s.record([record()], event=OBSERVED, environment="prod", channel=Channel.SCHEDULED)


def test_poller_channel_migrates_to_scheduled(tmp_path):
    import sqlite3

    from marshal_ledger import store as module

    path = tmp_path / "v3.sqlite"
    db = sqlite3.connect(path)
    db.executescript(module._SCHEMA_V1)
    db.executescript(module._MIGRATIONS[1])
    db.executescript(module._MIGRATIONS[2])
    db.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '3')")
    db.execute("INSERT INTO agents VALUES ('a', 'p', NULL, 't', 't')")
    db.execute("INSERT INTO versions VALUES ('a', 'sha256:x', 1, ?, 't')", (record().model_dump_json(by_alias=True),))
    db.execute("INSERT INTO events (agent, version, platform, environment, event, at, bindings, channel) "
               "VALUES ('a', 'sha256:x', 'p', 'prod', 'observed', ?, '{}', 'poller')", (T0.isoformat(),))
    db.commit()
    db.close()
    with Store(path) as s:
        assert [e.channel for e, _, _ in s.history("a")] == [Channel.SCHEDULED]


def test_postgres_store_at_version_3_migrates(ledger_location):
    if not ledger_location.startswith("postgresql://"):
        pytest.skip("Postgres migration (set MARSHAL_TEST_POSTGRES_URL)")
    import psycopg

    with Store(ledger_location) as s:
        s.record([record()], event=OBSERVED, environment="prod", at=T0, channel=Channel.SCHEDULED)
    with psycopg.connect(ledger_location, autocommit=True) as connection:
        connection.execute("ALTER TABLE events DROP CONSTRAINT events_channel_check")
        connection.execute("UPDATE events SET channel = 'poller'")
        connection.execute("UPDATE meta SET value = '3' WHERE key = 'schema_version'")
    with Store(ledger_location) as s:
        assert [e.channel for e, _, _ in s.history("finance/invoice")] == [Channel.SCHEDULED]
