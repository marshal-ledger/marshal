"""The version store: agents, versions and deployment events.

Every ``AgentVersion`` is stored once per (agent, version hash) and given a
per-agent sequence number (``v1``, ``v2``, ...) in the order the store first saw
it. Each collection adds a ``proposed`` or ``shipped`` event that places a
version in an environment. "Which version runs where" is the latest shipped
event per environment.

The store also indexes which entities each version references, so dependency
queries ("which agents use this tool?") don't need to parse records.
"""

from __future__ import annotations

import fnmatch
import json
import sqlite3
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

from .dbauth import PasswordProvider
from .freshness import FreshnessPolicy
from .model import AgentVersion, Channel, Deployment, DeploymentEvent, Entity

SCHEMA_VERSION = 4

_SCHEMA_V1 = """
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE agents (
    id           TEXT PRIMARY KEY,
    platform     TEXT NOT NULL,
    display_name TEXT,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL
);
CREATE TABLE versions (
    agent      TEXT NOT NULL REFERENCES agents(id),
    version    TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    record     TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    PRIMARY KEY (agent, version),
    UNIQUE (agent, seq)
);
CREATE TABLE events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    agent        TEXT NOT NULL,
    version      TEXT NOT NULL,
    platform     TEXT NOT NULL,
    environment  TEXT NOT NULL,
    event        TEXT NOT NULL CHECK (event IN ('proposed', 'shipped')),
    at           TEXT NOT NULL,
    commit_sha   TEXT,
    pipeline_run TEXT,
    artifact     TEXT,
    bindings     TEXT NOT NULL,
    FOREIGN KEY (agent, version) REFERENCES versions(agent, version)
);
CREATE INDEX events_by_agent ON events(agent, id);
CREATE TABLE entities (
    ref          TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    platform     TEXT NOT NULL,
    identifier   TEXT NOT NULL,
    display_name TEXT
);
CREATE TABLE version_refs (
    agent    TEXT NOT NULL,
    version  TEXT NOT NULL,
    ref      TEXT NOT NULL,
    relation TEXT NOT NULL CHECK (relation IN ('tool', 'knowledge', 'grant')),
    PRIMARY KEY (agent, version, ref, relation)
);
CREATE INDEX version_refs_by_ref ON version_refs(ref);
"""

# Postgres stores start at the current schema. Future migrations need a Postgres
# counterpart alongside each SQLite one.
_SCHEMA_POSTGRES = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE agents (
    id TEXT PRIMARY KEY, platform TEXT NOT NULL, display_name TEXT,
    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
);
CREATE TABLE versions (
    agent TEXT NOT NULL REFERENCES agents(id), version TEXT NOT NULL, seq INTEGER NOT NULL,
    record TEXT NOT NULL, first_seen TEXT NOT NULL,
    PRIMARY KEY (agent, version), UNIQUE (agent, seq)
);
CREATE TABLE events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    agent TEXT NOT NULL, version TEXT NOT NULL, platform TEXT NOT NULL, environment TEXT NOT NULL,
    event TEXT NOT NULL CHECK (event IN ('proposed', 'shipped', 'observed')),
    at TEXT NOT NULL, commit_sha TEXT, pipeline_run TEXT, artifact TEXT, bindings TEXT NOT NULL,
    actor_issuer TEXT, actor_repository TEXT, actor_ref TEXT, actor_workflow TEXT,
    channel TEXT NOT NULL DEFAULT 'manual' CONSTRAINT events_channel_check CHECK (channel IN ('ci', 'scheduled', 'manual')),
    FOREIGN KEY (agent, version) REFERENCES versions(agent, version)
);
CREATE INDEX events_by_agent ON events(agent, id);
CREATE TABLE entities (
    ref TEXT PRIMARY KEY, kind TEXT NOT NULL, platform TEXT NOT NULL, identifier TEXT NOT NULL, display_name TEXT
);
CREATE TABLE version_refs (
    agent TEXT NOT NULL, version TEXT NOT NULL, ref TEXT NOT NULL,
    relation TEXT NOT NULL CHECK (relation IN ('tool', 'knowledge', 'grant')),
    PRIMARY KEY (agent, version, ref, relation)
);
CREATE INDEX version_refs_by_ref ON version_refs(ref);
CREATE TABLE agent_owners (
    agent TEXT PRIMARY KEY REFERENCES agents(id), issuer TEXT NOT NULL, repository TEXT NOT NULL, bound_at TEXT NOT NULL
);
"""

# Each entry upgrades the schema from version N to N + 1.
_MIGRATIONS: dict[int, str] = {
    1: """
ALTER TABLE events ADD COLUMN actor_issuer TEXT;
ALTER TABLE events ADD COLUMN actor_repository TEXT;
ALTER TABLE events ADD COLUMN actor_ref TEXT;
ALTER TABLE events ADD COLUMN actor_workflow TEXT;
CREATE TABLE agent_owners (
    agent      TEXT PRIMARY KEY REFERENCES agents(id),
    issuer     TEXT NOT NULL,
    repository TEXT NOT NULL,
    bound_at   TEXT NOT NULL
);
""",
    # Adds the observed event and each event's collection channel. SQLite can't alter a
    # CHECK constraint, so the events table is rebuilt.
    2: """
CREATE TABLE events_v3 (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    agent            TEXT NOT NULL,
    version          TEXT NOT NULL,
    platform         TEXT NOT NULL,
    environment      TEXT NOT NULL,
    event            TEXT NOT NULL CHECK (event IN ('proposed', 'shipped', 'observed')),
    at               TEXT NOT NULL,
    commit_sha       TEXT,
    pipeline_run     TEXT,
    artifact         TEXT,
    bindings         TEXT NOT NULL,
    actor_issuer     TEXT,
    actor_repository TEXT,
    actor_ref        TEXT,
    actor_workflow   TEXT,
    channel          TEXT NOT NULL DEFAULT 'manual' CHECK (channel IN ('ci', 'poller', 'manual')),
    FOREIGN KEY (agent, version) REFERENCES versions(agent, version)
);
INSERT INTO events_v3 (id, agent, version, platform, environment, event, at, commit_sha, pipeline_run,
                       artifact, bindings, actor_issuer, actor_repository, actor_ref, actor_workflow, channel)
SELECT id, agent, version, platform, environment, event, at, commit_sha, pipeline_run, artifact, bindings,
       actor_issuer, actor_repository, actor_ref, actor_workflow,
       CASE WHEN actor_repository IS NOT NULL THEN 'ci' ELSE 'manual' END
FROM events;
DROP TABLE events;
ALTER TABLE events_v3 RENAME TO events;
CREATE INDEX events_by_agent ON events(agent, id);
""",
    # Renames the poller channel to scheduled.
    3: """
CREATE TABLE events_v4 (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    agent            TEXT NOT NULL,
    version          TEXT NOT NULL,
    platform         TEXT NOT NULL,
    environment      TEXT NOT NULL,
    event            TEXT NOT NULL CHECK (event IN ('proposed', 'shipped', 'observed')),
    at               TEXT NOT NULL,
    commit_sha       TEXT,
    pipeline_run     TEXT,
    artifact         TEXT,
    bindings         TEXT NOT NULL,
    actor_issuer     TEXT,
    actor_repository TEXT,
    actor_ref        TEXT,
    actor_workflow   TEXT,
    channel          TEXT NOT NULL DEFAULT 'manual' CHECK (channel IN ('ci', 'scheduled', 'manual')),
    FOREIGN KEY (agent, version) REFERENCES versions(agent, version)
);
INSERT INTO events_v4 SELECT id, agent, version, platform, environment, event, at, commit_sha, pipeline_run,
    artifact, bindings, actor_issuer, actor_repository, actor_ref, actor_workflow,
    CASE channel WHEN 'poller' THEN 'scheduled' ELSE channel END
FROM events;
DROP TABLE events;
ALTER TABLE events_v4 RENAME TO events;
CREATE INDEX events_by_agent ON events(agent, id);
""",
}

# Postgres stores are created at the current schema; each entry upgrades one from version N.
_MIGRATIONS_POSTGRES: dict[int, str] = {
    3: """
ALTER TABLE events DROP CONSTRAINT IF EXISTS events_channel_check;
UPDATE events SET channel = 'scheduled' WHERE channel = 'poller';
ALTER TABLE events ADD CONSTRAINT events_channel_check CHECK (channel IN ('ci', 'scheduled', 'manual'));
""",
}


class StoreError(LookupError):
    """A requested agent or version is not in the store."""


class StoreUnavailable(ConnectionError):
    """The store's database could not be reached, or the connection was lost.

    A write interrupted this way is rolled back by the database, unless the
    connection was lost while committing, when its outcome is unknown. Writes are
    never replayed. The next operation on the store opens a new connection.
    """


class OwnershipError(PermissionError):
    """A caller tried to record an agent that belongs to another repository."""


@dataclass(frozen=True)
class Actor:
    """The authenticated pipeline that recorded an event."""

    issuer: str
    repository: str
    ref: Optional[str] = None
    workflow: Optional[str] = None


@dataclass(frozen=True)
class StoredVersion:
    seq: int
    record: AgentVersion
    first_seen: datetime

    @property
    def label(self) -> str:
        return f"v{self.seq}"


@dataclass(frozen=True)
class Recorded:
    """The outcome of recording one agent version."""

    agent: str
    version: StoredVersion
    new_version: bool
    event: Deployment


@dataclass(frozen=True)
class EnvironmentStatus:
    """Where an agent stands in one environment: what is shipped, what is live, and how current that is."""

    environment: str
    shipped: Optional[int]
    """Sequence number of the version last shipped here."""
    observed: Optional[int]
    """Sequence number of the version last observed live here."""
    last_confirmed: datetime
    """Time of the latest shipped or observed event."""
    channel: Channel
    """Channel of that event."""
    age_days: float
    stale: bool
    drift: bool
    """The live definition differs from what was last shipped."""

    @property
    def summary(self) -> str:
        days = int(self.age_days)
        text = f"{self.channel.value} {days}d"
        if self.drift:
            text += " DRIFT"
        elif self.stale:
            text += " stale"
        return text


@dataclass(frozen=True)
class Dependent:
    """An agent version that references a matching entity."""

    agent: str
    platform: str
    label: str
    relation: str
    """``tool``, ``knowledge`` or ``grant``."""
    ref: str
    latest: bool
    shipped: tuple[str, ...]
    """Environments where this version is the one last shipped."""
    match: str = "exact"
    """``exact`` for a ref matching the pattern; otherwise how the ref resolves to the same thing,
    such as ``same HTTP operation`` (see :mod:`marshal_ledger.resolve`)."""
    via: Optional[str] = None
    """For a resolved match, the target the two refs share."""
    basis: Optional[str] = None
    """For a resolved match, how this ref's target was derived, with any sources that document it."""


@dataclass(frozen=True)
class AgentSummary:
    id: str
    platform: str
    display_name: Optional[str]
    latest: StoredVersion
    shipped: dict[str, int]
    """Environment → sequence number of the version last shipped there."""
    environments: tuple["EnvironmentStatus", ...] = ()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(at: datetime) -> str:
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at.astimezone(timezone.utc).isoformat()


def _refs(record: AgentVersion) -> Iterator[tuple[str, str]]:
    for tool in record.spec.tools:
        yield tool.ref, "tool"
    for item in record.spec.knowledge:
        yield item.ref, "knowledge"
    for grant in record.spec.permissions.grants:
        yield grant.target, "grant"


class Store:
    """A ledger store: SQLite for local use, Postgres for shared deployments.

    ``location`` is a SQLite file path (optionally ``sqlite:///path``) or a
    ``postgresql://`` URL. Postgres needs the ``postgres`` extra.

    ``password`` supplies the Postgres password each time the store connects,
    in place of one in the URL (see :mod:`marshal_ledger.dbauth`). A lost Postgres
    connection is replaced on the next operation; see :class:`StoreUnavailable`.
    """

    def __init__(self, location: str | Path, *, password: PasswordProvider | None = None):
        self.path = str(location)
        self._postgres = self.path.startswith(("postgresql://", "postgres://"))
        self._password = password
        self._tx_depth = 0
        if self._postgres:
            try:
                import psycopg
            except ImportError as exc:  # pragma: no cover - depends on the environment
                raise StoreError("Postgres stores need: pip install 'marshal-ledger[postgres]'") from exc
            self._operational_error = psycopg.OperationalError
            self._db = self._connect()
        else:
            if password is not None:
                raise ValueError("a password provider applies only to Postgres stores")
            self._db = sqlite3.connect(self.path.removeprefix("sqlite:///"))
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def _connect(self) -> Any:
        import psycopg
        from psycopg.rows import dict_row

        options: dict[str, Any] = {}
        if self._password is not None:
            # Overrides any password in the URL. Obtained per connection, so it can be a short-lived token.
            options["password"] = self._password()
        try:
            # Autocommit: reads run on their own, and each transaction block is a real, committed transaction.
            return psycopg.connect(self.path, row_factory=dict_row, autocommit=True, **options)
        except psycopg.OperationalError as exc:
            raise StoreUnavailable(f"cannot connect to the Postgres store: {exc}") from exc

    def _lost(self, exc: BaseException) -> bool:
        """Whether ``exc`` means the connection is gone, rather than a failed statement."""
        return isinstance(exc, self._operational_error) and self._db.closed

    def _reconnect_if_closed(self) -> None:
        if self._db.closed:
            self._db = self._connect()

    def _pg(self, operation: Callable[[Any], Any], *, read: bool) -> Any:
        """Run ``operation`` on the Postgres connection, reconnecting if it was lost.

        A read outside a transaction is retried once on a new connection. Losing the
        connection during anything else raises :class:`StoreUnavailable`, and the next
        operation reconnects.
        """
        if self._tx_depth == 0:
            self._reconnect_if_closed()
        try:
            return operation(self._db)
        except self._operational_error as exc:
            if not self._lost(exc):
                raise
            if not read or self._tx_depth:
                raise StoreUnavailable(f"lost the connection to the Postgres store: {exc}") from exc
        self._db = self._connect()
        try:
            return operation(self._db)
        except self._operational_error as exc:
            if self._lost(exc):
                raise StoreUnavailable(f"lost the connection to the Postgres store: {exc}") from exc
            raise

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self._postgres else sql

    def _q(self, sql: str, params: Iterable[Any] = ()) -> Any:
        params = tuple(params)
        if not self._postgres:
            return self._db.execute(sql, params)
        # Client-side cursors hold the whole result, so fetching afterwards needs no connection.
        return self._pg(lambda db: db.execute(self._sql(sql), params), read=sql.lstrip()[:6].upper() == "SELECT")

    def _qm(self, sql: str, rows: list[tuple[Any, ...]]) -> None:
        if self._postgres:
            def run(db: Any) -> None:
                with db.cursor() as cursor:
                    cursor.executemany(self._sql(sql), rows)

            self._pg(run, read=False)
        else:
            self._db.executemany(sql, rows)

    def _script(self, sql: str) -> None:
        """Run a multi-statement script inside a transaction block."""
        self._pg(lambda db: db.execute(sql), read=False)

    @contextmanager
    def _tx(self) -> Iterator[None]:
        if not self._postgres:
            with self._db:
                yield
            return
        try:
            with ExitStack() as stack:
                self._begin(stack)
                self._tx_depth += 1
                try:
                    yield
                finally:
                    self._tx_depth -= 1
        except self._operational_error as exc:
            if self._lost(exc):
                raise StoreUnavailable(f"lost the connection to the Postgres store: {exc}") from exc
            raise

    def _begin(self, stack: ExitStack) -> None:
        if self._tx_depth:
            stack.enter_context(self._db.transaction())
            return
        self._reconnect_if_closed()
        try:
            stack.enter_context(self._db.transaction())
        except self._operational_error as exc:
            # Lost before BEGIN: nothing has been sent, so a new connection may start the transaction.
            if not self._lost(exc):
                raise
            self._db = self._connect()
            stack.enter_context(self._db.transaction())

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _migrate(self) -> None:
        if self._postgres:
            self._migrate_postgres()
            return
        has_meta = self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
        ).fetchone()
        if not has_meta:
            self._db.executescript(_SCHEMA_V1)
            self._db.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '1')")
            self._db.commit()
        row = self._db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        found = int(row["value"]) if row else 0
        if found > SCHEMA_VERSION or found < 1:
            raise StoreError(f"{self.path}: store schema version {found}, expected {SCHEMA_VERSION}")
        for version in range(found, SCHEMA_VERSION):
            # executescript commits first, so each migration runs as its own script.
            self._db.executescript(
                "BEGIN;\n" + _MIGRATIONS[version]
                + f"\nUPDATE meta SET value = '{version + 1}' WHERE key = 'schema_version';\nCOMMIT;"
            )

    def _migrate_postgres(self) -> None:
        exists = self._q("SELECT to_regclass('meta') AS meta").fetchone()["meta"]
        if exists is None:
            with self._tx():
                self._script(_SCHEMA_POSTGRES)
                self._q("INSERT INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            return
        row = self._q("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        found = int(row["value"]) if row else 0
        if found > SCHEMA_VERSION or found not in _MIGRATIONS_POSTGRES and found != SCHEMA_VERSION:
            raise StoreError(f"store schema version {found}, expected {SCHEMA_VERSION}")
        for version in range(found, SCHEMA_VERSION):
            with self._tx():
                self._script(_MIGRATIONS_POSTGRES[version])
                self._q("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(version + 1),))

    # -- writes ----------------------------------------------------------

    def record(
        self,
        records: Iterable[AgentVersion],
        *,
        event: DeploymentEvent,
        environment: str,
        entities: Iterable[Entity] = (),
        at: datetime | None = None,
        actor: Actor | None = None,
        channel: Channel = Channel.MANUAL,
    ) -> list[Recorded]:
        """Store each record's version, if new, and add one event per record.

        With an ``actor``, each agent is bound to the actor's repository the first
        time it is recorded, and later writes from any other repository raise
        :class:`OwnershipError`. Nothing is written if any agent is refused.
        """
        records = list(records)
        when = _iso(at or _now())
        results: list[Recorded] = []
        with self._tx():
            if actor is not None:
                self._check_owners(records, actor, when)
            for entity in entities:
                self._q(
                    "INSERT INTO entities (ref, kind, platform, identifier, display_name) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT (ref) DO UPDATE SET display_name = COALESCE(excluded.display_name, entities.display_name)",
                    (entity.ref, entity.kind.value, entity.platform, entity.identifier, entity.display_name),
                )
            for record in records:
                results.append(self._record_one(record, event, environment, when, actor, channel))
        return results

    def _check_owners(self, records: list[AgentVersion], actor: Actor, when: str) -> None:
        refused = []
        for record in records:
            row = self._q(
                "SELECT issuer, repository FROM agent_owners WHERE agent = ?", (record.agent,)
            ).fetchone()
            if row is not None and (row["issuer"], row["repository"]) != (actor.issuer, actor.repository):
                refused.append(f"{record.agent} belongs to {row['repository']}")
        if refused:
            raise OwnershipError("; ".join(refused))

    def owner(self, agent: str) -> Optional[tuple[str, str]]:
        """The (issuer, repository) an agent is bound to, if any."""
        row = self._q("SELECT issuer, repository FROM agent_owners WHERE agent = ?", (agent,)).fetchone()
        return (row["issuer"], row["repository"]) if row else None

    def transfer(self, agent: str, issuer: str, repository: str) -> None:
        """Bind ``agent`` to another repository. An administrative operation."""
        if not self.has_agent(agent):
            raise StoreError(f"unknown agent {agent!r}")
        with self._tx():
            self._q(
                "INSERT INTO agent_owners (agent, issuer, repository, bound_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (agent) DO UPDATE SET issuer = excluded.issuer, "
                "repository = excluded.repository, bound_at = excluded.bound_at",
                (agent, issuer, repository, _iso(_now())),
            )

    def _record_one(
        self, record: AgentVersion, event: DeploymentEvent, environment: str, when: str, actor: Actor | None,
        channel: Channel = Channel.MANUAL,
    ) -> Recorded:
        display = record.spec.display_name.value
        self._q(
            "INSERT INTO agents (id, platform, display_name, first_seen, last_seen) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET last_seen = excluded.last_seen, "
            "display_name = COALESCE(excluded.display_name, agents.display_name)",
            (record.agent, record.platform, display, when, when),
        )
        if actor is not None:
            self._q(
                "INSERT INTO agent_owners (agent, issuer, repository, bound_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (agent) DO NOTHING",
                (record.agent, actor.issuer, actor.repository, when),
            )
        if self._postgres:
            self._q("SELECT id FROM agents WHERE id = ? FOR UPDATE", (record.agent,))
        row = self._q(
            "SELECT seq FROM versions WHERE agent = ? AND version = ?", (record.agent, record.version)
        ).fetchone()
        new_version = row is None
        if new_version:
            seq = self._q(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM versions WHERE agent = ?", (record.agent,)
            ).fetchone()["next"]
            # Bindings and source belong to the event, not the version.
            stored = record.model_copy(update={"bindings": {}})
            self._q(
                "INSERT INTO versions (agent, version, seq, record, first_seen) VALUES (?, ?, ?, ?, ?)",
                (record.agent, record.version, seq, stored.model_dump_json(by_alias=True), when),
            )
            self._qm(
                "INSERT INTO version_refs (agent, version, ref, relation) VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                [(record.agent, record.version, ref, relation) for ref, relation in _refs(record)],
            )
        else:
            seq = row["seq"]
        self._q(
            "INSERT INTO events (agent, version, platform, environment, event, at, commit_sha, "
            "pipeline_run, artifact, bindings, actor_issuer, actor_repository, actor_ref, actor_workflow, channel) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.agent,
                record.version,
                record.platform,
                environment,
                event.value,
                when,
                record.source.commit,
                record.source.pipeline_run,
                record.source.artifact,
                json.dumps(record.bindings, sort_keys=True),
                actor.issuer if actor else None,
                actor.repository if actor else None,
                actor.ref if actor else None,
                actor.workflow if actor else None,
                channel.value,
            ),
        )
        stored_version = self._version_row(record.agent, "seq = ?", (seq,))
        deployment = Deployment(
            agent=record.agent,
            version=record.version,
            platform=record.platform,
            environment=environment,
            event=event,
            at=datetime.fromisoformat(when),
            channel=channel,
            commit=record.source.commit,
            pipeline_run=record.source.pipeline_run,
        )
        return Recorded(agent=record.agent, version=stored_version, new_version=new_version, event=deployment)

    # -- reads -----------------------------------------------------------

    def _version_row(self, agent: str, where: str, params: tuple) -> StoredVersion:
        row = self._q(
            f"SELECT seq, record, first_seen FROM versions WHERE agent = ? AND {where}", (agent, *params)
        ).fetchone()
        if row is None:
            raise StoreError(f"{agent}: no matching version")
        return StoredVersion(
            seq=row["seq"],
            record=AgentVersion.model_validate_json(row["record"]),
            first_seen=datetime.fromisoformat(row["first_seen"]),
        )

    def has_agent(self, agent: str) -> bool:
        return self._q("SELECT 1 FROM agents WHERE id = ?", (agent,)).fetchone() is not None

    def resolve(self, agent: str, selector: str = "latest") -> StoredVersion:
        """Find one version of ``agent``.

        Selectors: ``vN``; a version hash or a prefix of at least 7 hex digits;
        ``latest`` (the version in the most recent event); ``shipped`` or
        ``proposed``, optionally with ``:<environment>``, for the version in the
        most recent event of that kind.
        """
        if not self.has_agent(agent):
            raise StoreError(f"unknown agent {agent!r}")
        if selector.startswith("v") and selector[1:].isdigit():
            return self._version_row(agent, "seq = ?", (int(selector[1:]),))
        if selector == "latest":
            return self._from_event(agent, None, None)
        kind, _, environment = selector.partition(":")
        if kind in {e.value for e in DeploymentEvent}:
            return self._from_event(agent, kind, environment or None)
        digest = selector.removeprefix("sha256:").lower()
        if len(digest) >= 7 and all(c in "0123456789abcdef" for c in digest):
            rows = self._q(
                "SELECT seq FROM versions WHERE agent = ? AND version LIKE ?", (agent, f"sha256:{digest}%")
            ).fetchall()
            if len(rows) == 1:
                return self._version_row(agent, "seq = ?", (rows[0]["seq"],))
            if len(rows) > 1:
                raise StoreError(f"{agent}@{selector}: ambiguous version prefix")
        raise StoreError(f"{agent}@{selector}: no matching version")

    def _from_event(self, agent: str, kind: Optional[str], environment: Optional[str]) -> StoredVersion:
        sql = "SELECT version FROM events WHERE agent = ?"
        params: list[str] = [agent]
        if kind:
            sql += " AND event = ?"
            params.append(kind)
        if environment:
            sql += " AND environment = ?"
            params.append(environment)
        row = self._q(sql + " ORDER BY id DESC LIMIT 1", params).fetchone()
        if row is None:
            where = " ".join(filter(None, [kind, f"in {environment}" if environment else None]))
            raise StoreError(f"{agent}: no {where or 'recorded'} version")
        return self._version_row(agent, "version = ?", (row["version"],))

    def history(self, agent: str) -> list[tuple[Deployment, int, Optional[Actor]]]:
        """Every event for ``agent``, oldest first, with the version's sequence number and actor."""
        if not self.has_agent(agent):
            raise StoreError(f"unknown agent {agent!r}")
        rows = self._q(
            "SELECT e.*, v.seq FROM events e JOIN versions v ON v.agent = e.agent AND v.version = e.version "
            "WHERE e.agent = ? ORDER BY e.id",
            (agent,),
        ).fetchall()
        return [
            (
                Deployment(
                    agent=row["agent"],
                    version=row["version"],
                    platform=row["platform"],
                    environment=row["environment"],
                    event=DeploymentEvent(row["event"]),
                    at=datetime.fromisoformat(row["at"]),
                    channel=Channel(row["channel"]),
                    commit=row["commit_sha"],
                    pipeline_run=row["pipeline_run"],
                ),
                row["seq"],
                Actor(row["actor_issuer"], row["actor_repository"], row["actor_ref"], row["actor_workflow"])
                if row["actor_repository"]
                else None,
            )
            for row in rows
        ]

    def agents(
        self,
        *,
        platform: str | None = None,
        tool: str | None = None,
        knowledge: str | None = None,
        provider: str | None = None,
        identity: str | None = None,
        policy: FreshnessPolicy | None = None,
        now: datetime | None = None,
    ) -> list[AgentSummary]:
        """Every agent with its latest version. ``tool`` and ``knowledge`` are glob patterns
        matched against the latest version's references."""
        rows = self._q("SELECT id, platform, display_name FROM agents ORDER BY id").fetchall()
        summaries = []
        for row in rows:
            if platform and row["platform"] != platform:
                continue
            latest = self._from_event(row["id"], None, None)
            if tool and not self._uses(latest.record, "tool", tool):
                continue
            if knowledge and not self._uses(latest.record, "knowledge", knowledge):
                continue
            spec = latest.record.spec
            if provider and not fnmatch.fnmatchcase(str(spec.model.provider.value or "").lower(), provider.lower()):
                continue
            if identity and (not spec.permissions.identity.is_known or spec.permissions.identity.value.value != identity):
                continue
            shipped = {
                r["environment"]: r["seq"]
                for r in self._q(
                    "SELECT e.environment, v.seq FROM events e "
                    "JOIN versions v ON v.agent = e.agent AND v.version = e.version "
                    "WHERE e.agent = ? AND e.event = 'shipped' "
                    "AND e.id = (SELECT MAX(id) FROM events x WHERE x.agent = e.agent "
                    "AND x.environment = e.environment AND x.event = 'shipped') "
                    "ORDER BY e.environment",
                    (row["id"],),
                )
            }
            summaries.append(
                AgentSummary(
                    id=row["id"],
                    platform=row["platform"],
                    display_name=row["display_name"],
                    latest=latest,
                    shipped=shipped,
                    environments=tuple(self.status(row["id"], policy=policy, now=now)),
                )
            )
        return summaries

    def status(
        self, agent: str, *, policy: FreshnessPolicy | None = None, now: datetime | None = None
    ) -> list[EnvironmentStatus]:
        """Per environment: the shipped and observed versions, drift between them, and freshness."""
        if not self.has_agent(agent):
            raise StoreError(f"unknown agent {agent!r}")
        policy = policy or FreshnessPolicy()
        now = now or _now()
        rows = self._q(
            "SELECT e.id, e.environment, e.event, e.version, e.at, e.channel, v.seq FROM events e "
            "JOIN versions v ON v.agent = e.agent AND v.version = e.version "
            "WHERE e.agent = ? AND e.event IN ('shipped', 'observed') ORDER BY e.id",
            (agent,),
        ).fetchall()
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            latest.setdefault(row["environment"], {})[row["event"]] = row
        statuses = []
        for environment in sorted(latest):
            events = latest[environment]
            shipped, observed = events.get("shipped"), events.get("observed")
            newest = max((r for r in (shipped, observed) if r is not None), key=lambda r: r["id"])
            confirmed = datetime.fromisoformat(newest["at"])
            age = (now - confirmed).total_seconds() / 86400
            channel = Channel(newest["channel"])
            drift = bool(
                shipped is not None and observed is not None
                and observed["id"] > shipped["id"] and observed["version"] != shipped["version"]
            )
            statuses.append(EnvironmentStatus(
                environment=environment,
                shipped=shipped["seq"] if shipped is not None else None,
                observed=observed["seq"] if observed is not None else None,
                last_confirmed=confirmed,
                channel=channel,
                age_days=max(age, 0.0),
                stale=policy.is_stale(channel, age),
                drift=drift,
            ))
        return statuses

    def _uses(self, record: AgentVersion, relation: str, pattern: str) -> bool:
        return bool(self._matching_refs(record.agent, record.version, pattern, relation))

    def _matching_refs(self, agent: str, version: str, pattern: str, relation: str | None) -> list[tuple[str, str]]:
        """(ref, relation) pairs of one version whose ref, its identifier, or the ref without its kind match ``pattern``."""
        sql = ("SELECT r.ref, r.relation, n.identifier FROM version_refs r LEFT JOIN entities n ON n.ref = r.ref "
               "WHERE r.agent = ? AND r.version = ?")
        params: list[str] = [agent, version]
        if relation:
            sql += " AND r.relation = ?"
            params.append(relation)
        pattern = pattern.lower()
        found = []
        for row in self._q(sql, params).fetchall():
            ref = row["ref"].lower()
            candidates = [ref, ref.split("/", 1)[-1]]
            if row["identifier"]:
                candidates.append(row["identifier"].lower())
            if any(fnmatch.fnmatchcase(c, pattern) for c in candidates):
                found.append((row["ref"], row["relation"]))
        return found

    def _dependency_versions(self, shipped_only: bool) -> Iterator[tuple[str, str, StoredVersion, bool, tuple[str, ...]]]:
        """(agent, platform, version, is latest, environments shipped to) for every agent's latest and shipped versions."""
        for row in self._q("SELECT id, platform FROM agents ORDER BY id").fetchall():
            agent = row["id"]
            latest = self._from_event(agent, None, None)
            shipped: dict[str, list[str]] = {}
            for env_row in self._q(
                "SELECT e.environment, e.version FROM events e WHERE e.agent = ? AND e.event = 'shipped' "
                "AND e.id = (SELECT MAX(id) FROM events x WHERE x.agent = e.agent AND x.environment = e.environment "
                "AND x.event = 'shipped')", (agent,)
            ):
                shipped.setdefault(env_row["version"], []).append(env_row["environment"])
            versions = set(shipped) | ({latest.record.version} if not shipped_only else set())
            for version in sorted(versions):
                stored = latest if version == latest.record.version else self._version_row(agent, "version = ?", (version,))
                yield agent, row["platform"], stored, version == latest.record.version, tuple(sorted(shipped.get(version, [])))

    def _with_event_bindings(self, agent: str, record: AgentVersion) -> AgentVersion:
        """The record with the executors its events recorded, in every environment, as lists per ref.

        Bindings belong to events, so a version shipped to two environments may run on two functions.
        """
        executors: dict[str, list[str]] = {}
        for row in self._q("SELECT bindings FROM events WHERE agent = ? AND version = ?", (agent, record.version)):
            found = json.loads(row["bindings"] or "{}").get("executors")
            for ref, executor in (found.items() if isinstance(found, dict) else ()):
                if executor not in executors.setdefault(ref, []):
                    executors[ref].append(executor)
        return record.model_copy(update={"bindings": {"executors": executors}}) if executors else record

    def dependents(self, pattern: str, *, relation: str | None = None, shipped_only: bool = False) -> list[Dependent]:
        """Agents whose latest or shipped versions reference an entity matching ``pattern`` (a glob)."""
        out: list[Dependent] = []
        for agent, platform, stored, latest, shipped in self._dependency_versions(shipped_only):
            for ref, rel in self._matching_refs(agent, stored.record.version, pattern, relation):
                out.append(Dependent(agent, platform, stored.label, rel, ref, latest=latest, shipped=shipped))
        return out

    def related(
        self, pattern: str, *, relation: str | None = None, shipped_only: bool = False, exact: list[Dependent] | None = None
    ) -> list[Dependent]:
        """Other refs that reach what the refs matching ``pattern`` reach, on any platform.

        A ref matches through a shared target (see :mod:`marshal_ledger.resolve`): the same HTTP
        operation, MCP server, OpenAPI operation or knowledge URL first, then the same API host.
        ``pattern`` may also name targets directly, such as ``host:api.acme.example``. Refs that
        ``dependents`` already returns (pass them as ``exact``) aren't repeated.
        """
        from .resolve import attestation, label, links, strength, targets

        if exact is None:
            exact = self.dependents(pattern, relation=relation, shipped_only=shipped_only)
        seen = {(d.agent, d.label, d.ref) for d in exact}
        candidates = list(self._dependency_versions(shipped_only))
        # Links a person stated in a Marshal file apply to every version that uses the linked ref.
        attested: dict[str, list[tuple[str, str]]] = {}
        for _, _, stored, _, _ in candidates:
            for ref, target in links(stored.record):
                if (target, who := attestation(stored.record)) not in attested.setdefault(ref, []):
                    attested[ref].append((target, who))
        versions = [(v, targets(self._with_event_bindings(v[0], v[2].record), attested)) for v in candidates]
        # Target → the strongest evidence any matched ref has for it: "" (stated) beats "attested" beats "documented".
        wanted: dict[str, str] = {}
        order = {"": 0, "attested": 1, "documented": 2}
        direct = pattern.lower().split(":", 1)[0] in ("http", "mcp", "openapi", "url", "host", "lambda")
        for (agent, _, stored, _, _), found in versions:
            for t in found:
                if (agent, stored.label, t.ref) in seen or (direct and fnmatch.fnmatchcase(t.target.lower(), pattern.lower())):
                    current = wanted.get(t.target)
                    wanted[t.target] = t.qualifier if current is None else min(current, t.qualifier, key=order.__getitem__)
        best: dict[tuple[str, str, str], Dependent] = {}
        for (agent, platform, stored, latest, shipped), found in versions:
            for t in found:
                key = (agent, stored.label, t.ref)
                if t.target not in wanted or key in seen or (relation and t.relation != relation):
                    continue
                if key not in best or strength(t.target) > strength(best[key].via or ""):
                    basis = t.basis + (": " + ", ".join(t.sources) if t.sources else "") + (f", {t.attested}" if t.attested else "")
                    match = label(t, wanted[t.target])
                    best[key] = Dependent(agent, platform, stored.label, t.relation, t.ref, latest=latest, shipped=shipped,
                                          match=match, via=t.target, basis=basis)
        return sorted(best.values(), key=lambda d: (-strength(d.via or ""), d.agent, d.label, d.ref))

    def entities(self, kind: str | None = None) -> list[tuple[Entity, int]]:
        """Every known entity, with the number of agents whose latest version references it."""
        from .model import EntityKind

        latest = {}
        for row in self._q("SELECT id FROM agents").fetchall():
            latest[row["id"]] = self._from_event(row["id"], None, None).record.version
        counts: dict[str, set[str]] = {}
        for row in self._q("SELECT agent, version, ref FROM version_refs").fetchall():
            if latest.get(row["agent"]) == row["version"]:
                counts.setdefault(row["ref"], set()).add(row["agent"])
        sql = "SELECT ref, kind, platform, identifier, display_name FROM entities"
        params: list[str] = []
        if kind:
            sql += " WHERE kind = ?"
            params.append(kind)
        result = []
        for row in self._q(sql + " ORDER BY ref", params).fetchall():
            entity = Entity(ref=row["ref"], kind=EntityKind(row["kind"]), platform=row["platform"],
                            identifier=row["identifier"], display_name=row["display_name"])
            result.append((entity, len(counts.get(row["ref"], ()))))
        return result
