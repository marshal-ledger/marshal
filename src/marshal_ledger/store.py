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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from .freshness import FreshnessPolicy
from .model import AgentVersion, Channel, Deployment, DeploymentEvent, Entity

SCHEMA_VERSION = 3

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
}


class StoreError(LookupError):
    """A requested agent or version is not in the store."""


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
    """A ledger store backed by SQLite."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._db = sqlite3.connect(self.path)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _migrate(self) -> None:
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
        with self._db:
            if actor is not None:
                self._check_owners(records, actor, when)
            for entity in entities:
                self._db.execute(
                    "INSERT INTO entities (ref, kind, platform, identifier, display_name) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT (ref) DO UPDATE SET display_name = COALESCE(excluded.display_name, display_name)",
                    (entity.ref, entity.kind.value, entity.platform, entity.identifier, entity.display_name),
                )
            for record in records:
                results.append(self._record_one(record, event, environment, when, actor, channel))
        return results

    def _check_owners(self, records: list[AgentVersion], actor: Actor, when: str) -> None:
        refused = []
        for record in records:
            row = self._db.execute(
                "SELECT issuer, repository FROM agent_owners WHERE agent = ?", (record.agent,)
            ).fetchone()
            if row is not None and (row["issuer"], row["repository"]) != (actor.issuer, actor.repository):
                refused.append(f"{record.agent} belongs to {row['repository']}")
        if refused:
            raise OwnershipError("; ".join(refused))

    def owner(self, agent: str) -> Optional[tuple[str, str]]:
        """The (issuer, repository) an agent is bound to, if any."""
        row = self._db.execute("SELECT issuer, repository FROM agent_owners WHERE agent = ?", (agent,)).fetchone()
        return (row["issuer"], row["repository"]) if row else None

    def transfer(self, agent: str, issuer: str, repository: str) -> None:
        """Bind ``agent`` to another repository. An administrative operation."""
        if not self.has_agent(agent):
            raise StoreError(f"unknown agent {agent!r}")
        with self._db:
            self._db.execute(
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
        self._db.execute(
            "INSERT INTO agents (id, platform, display_name, first_seen, last_seen) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET last_seen = excluded.last_seen, "
            "display_name = COALESCE(excluded.display_name, display_name)",
            (record.agent, record.platform, display, when, when),
        )
        if actor is not None:
            self._db.execute(
                "INSERT OR IGNORE INTO agent_owners (agent, issuer, repository, bound_at) VALUES (?, ?, ?, ?)",
                (record.agent, actor.issuer, actor.repository, when),
            )
        row = self._db.execute(
            "SELECT seq FROM versions WHERE agent = ? AND version = ?", (record.agent, record.version)
        ).fetchone()
        new_version = row is None
        if new_version:
            seq = self._db.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM versions WHERE agent = ?", (record.agent,)
            ).fetchone()[0]
            # Bindings and source belong to the event, not the version.
            stored = record.model_copy(update={"bindings": {}})
            self._db.execute(
                "INSERT INTO versions (agent, version, seq, record, first_seen) VALUES (?, ?, ?, ?, ?)",
                (record.agent, record.version, seq, stored.model_dump_json(by_alias=True), when),
            )
            self._db.executemany(
                "INSERT OR IGNORE INTO version_refs (agent, version, ref, relation) VALUES (?, ?, ?, ?)",
                [(record.agent, record.version, ref, relation) for ref, relation in _refs(record)],
            )
        else:
            seq = row["seq"]
        self._db.execute(
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
        row = self._db.execute(
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
        return self._db.execute("SELECT 1 FROM agents WHERE id = ?", (agent,)).fetchone() is not None

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
            rows = self._db.execute(
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
        row = self._db.execute(sql + " ORDER BY id DESC LIMIT 1", params).fetchone()
        if row is None:
            where = " ".join(filter(None, [kind, f"in {environment}" if environment else None]))
            raise StoreError(f"{agent}: no {where or 'recorded'} version")
        return self._version_row(agent, "version = ?", (row["version"],))

    def history(self, agent: str) -> list[tuple[Deployment, int, Optional[Actor]]]:
        """Every event for ``agent``, oldest first, with the version's sequence number and actor."""
        if not self.has_agent(agent):
            raise StoreError(f"unknown agent {agent!r}")
        rows = self._db.execute(
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
        rows = self._db.execute("SELECT id, platform, display_name FROM agents ORDER BY id").fetchall()
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
                for r in self._db.execute(
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
        rows = self._db.execute(
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
        for row in self._db.execute(sql, params).fetchall():
            ref = row["ref"].lower()
            candidates = [ref, ref.split("/", 1)[-1]]
            if row["identifier"]:
                candidates.append(row["identifier"].lower())
            if any(fnmatch.fnmatchcase(c, pattern) for c in candidates):
                found.append((row["ref"], row["relation"]))
        return found

    def dependents(self, pattern: str, *, relation: str | None = None, shipped_only: bool = False) -> list[Dependent]:
        """Agents whose latest or shipped versions reference an entity matching ``pattern`` (a glob)."""
        out: list[Dependent] = []
        for row in self._db.execute("SELECT id, platform FROM agents ORDER BY id").fetchall():
            agent = row["id"]
            latest = self._from_event(agent, None, None)
            shipped: dict[str, list[str]] = {}
            for env_row in self._db.execute(
                "SELECT e.environment, e.version FROM events e WHERE e.agent = ? AND e.event = 'shipped' "
                "AND e.id = (SELECT MAX(id) FROM events x WHERE x.agent = e.agent AND x.environment = e.environment "
                "AND x.event = 'shipped')", (agent,)
            ):
                shipped.setdefault(env_row["version"], []).append(env_row["environment"])
            versions = set(shipped) | ({latest.record.version} if not shipped_only else set())
            for version in sorted(versions):
                label = self._version_row(agent, "version = ?", (version,)).label
                for ref, rel in self._matching_refs(agent, version, pattern, relation):
                    out.append(Dependent(agent, row["platform"], label, rel, ref,
                                         latest=version == latest.record.version,
                                         shipped=tuple(sorted(shipped.get(version, [])))))
        return out

    def entities(self, kind: str | None = None) -> list[tuple[Entity, int]]:
        """Every known entity, with the number of agents whose latest version references it."""
        from .model import EntityKind

        latest = {}
        for row in self._db.execute("SELECT id FROM agents").fetchall():
            latest[row["id"]] = self._from_event(row["id"], None, None).record.version
        counts: dict[str, set[str]] = {}
        for row in self._db.execute("SELECT agent, version, ref FROM version_refs").fetchall():
            if latest.get(row["agent"]) == row["version"]:
                counts.setdefault(row["ref"], set()).add(row["agent"])
        sql = "SELECT ref, kind, platform, identifier, display_name FROM entities"
        params: list[str] = []
        if kind:
            sql += " WHERE kind = ?"
            params.append(kind)
        result = []
        for row in self._db.execute(sql + " ORDER BY ref", params).fetchall():
            entity = Entity(ref=row["ref"], kind=EntityKind(row["kind"]), platform=row["platform"],
                            identifier=row["identifier"], display_name=row["display_name"])
            result.append((entity, len(counts.get(row["ref"], ()))))
        return result
