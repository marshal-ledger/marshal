"""The ledger model, version 0.

Every reader maps its format into these types and every writer maps out of
them. The model is a superset of the formats it translates: alongside the agent
definition it holds where each value came from, which values are unknown, which
values belong to an environment rather than to the definition, and
platform-specific fields that have no portable equivalent.

The JSON Schema in ``schema/marshal-v0.schema.json`` is generated from these
models (see ``scripts/generate_schema.py``).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Generic, Literal, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field

API_VERSION = "marshal/v0"

MARSHAL_NAMESPACE = "io.github.marshal-ledger"
"""Key for Marshal's own extension block in formats that allow vendor extensions.
Writers use it only to carry what the target format can't name, such as the
original agent id and entity references, so a converted document reads back
as the same agent."""

T = TypeVar("T")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Provenance(str, Enum):
    """Where a value came from."""

    DECLARED = "declared"
    """Stated in the source artifact."""
    ANNOTATION = "annotation"
    """Taken from a hint attached to the source, such as MCP tool annotations."""
    DERIVED = "derived"
    """Computed by the ledger from other declared values, by a documented rule."""
    UNKNOWN = "unknown"
    """Not available from the source. The ledger does not guess."""


class Value(_Model, Generic[T]):
    """A single field value with its provenance.

    ``basis`` says where the value was found (a file and path) or which rule
    derived it. It is descriptive metadata and does not affect the version hash.
    """

    value: Optional[T] = None
    provenance: Provenance = Provenance.UNKNOWN
    basis: Optional[str] = None

    @classmethod
    def declared(cls, value: T, basis: str | None = None) -> "Value[T]":
        return cls(value=value, provenance=Provenance.DECLARED, basis=basis)

    @classmethod
    def derived(cls, value: T, basis: str) -> "Value[T]":
        return cls(value=value, provenance=Provenance.DERIVED, basis=basis)

    @classmethod
    def unknown(cls, basis: str | None = None) -> "Value[T]":
        return cls(value=None, provenance=Provenance.UNKNOWN, basis=basis)

    @property
    def is_known(self) -> bool:
        return self.provenance is not Provenance.UNKNOWN


class Scope(str, Enum):
    """What a tool or grant lets the agent do to the target."""

    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    """Runs code or a function whose side effects are not declared."""
    ADMIN = "admin"


class EntityKind(str, Enum):
    TOOL = "tool"
    KNOWLEDGE = "knowledge"
    MODEL = "model"
    AGENT = "agent"
    STORE = "store"
    """State the agent reads and writes, such as a memory database."""
    COMPUTE = "compute"
    SECRET = "secret"
    OTHER = "other"


class Entity(_Model):
    """A shared record that several agents can depend on.

    ``ref`` is ``<kind>/<platform-qualified identifier>``, for example
    ``knowledge/databricks.uc.table.samples.nyctaxi.trips``. In v0, two
    references are the same entity only when their refs match exactly.
    """

    ref: str
    kind: EntityKind
    platform: str
    identifier: str
    display_name: Optional[str] = None


class ModelSpec(_Model):
    requested: Value[str] = Field(default_factory=Value[str].unknown)
    """The model or model alias the agent asks for, as the source names it."""
    provider: Value[str] = Field(default_factory=Value[str].unknown)
    """Who serves the model, in lowercase: ``openai``, ``anthropic``, ``databricks``, ``oci``."""
    resolved: Value[str] = Field(default_factory=Value[str].unknown)
    """The concrete model that served requests. Filled only where a gateway is connected."""
    parameters: dict[str, Any] = Field(default_factory=dict)
    """Declared generation parameters, such as ``temperature`` and ``max_tokens``."""


class Instructions(_Model):
    hash: Optional[str] = None
    """``sha256:`` digest of the instruction text. Present whenever the text is known."""
    text: Optional[str] = None
    """The instruction text. Omitted in hash-only mode; never part of the version hash."""
    provenance: Provenance = Provenance.UNKNOWN
    basis: Optional[str] = None


class ToolUse(_Model):
    ref: str
    name: Optional[str] = None
    """The name the agent calls the tool by, where it differs from the entity."""
    description: Optional[str] = None
    input_schema: Optional[dict[str, Any]] = None
    """JSON Schema of the tool's input."""
    output_schema: Optional[dict[str, Any]] = None
    scope: Value[Scope] = Field(default_factory=Value[Scope].unknown)
    reversible: Value[bool] = Field(default_factory=Value[bool].unknown)
    requires_confirmation: Value[bool] = Field(default_factory=Value[bool].unknown)
    """Whether a human must approve each call."""


class KnowledgeUse(_Model):
    ref: str
    name: Optional[str] = None
    description: Optional[str] = None
    type: Optional[str] = None
    """The source's own kind, such as ``table``, ``vector_store`` or ``file``."""
    scope: Value[Scope] = Field(default_factory=Value[Scope].unknown)


class IdentityKind(str, Enum):
    SERVICE_PRINCIPAL = "service_principal"
    """The agent acts as its own non-human identity."""
    ON_BEHALF_OF_USER = "on_behalf_of_user"
    """The agent acts with the calling user's permissions."""
    SHARED_CREDENTIAL = "shared_credential"
    """The agent acts with credentials its author configured, the same for every user."""
    MIXED = "mixed"


class Grant(_Model):
    """A permission held by the agent's identity on a target."""

    target: str
    """Entity ref of the target."""
    privilege: str
    """The platform's own privilege name, such as ``CAN_QUERY`` or ``SELECT``."""
    scope: Scope
    provenance: Provenance = Provenance.DECLARED
    basis: Optional[str] = None


class AccessRule(_Model):
    """Who may use or manage the agent itself."""

    principal: str
    principal_type: Literal["user", "group", "service_principal", "unknown"]
    level: str


class Permissions(_Model):
    identity: Value[IdentityKind] = Field(default_factory=Value[IdentityKind].unknown)
    principal: Value[str] = Field(default_factory=Value[str].unknown)
    grants: list[Grant] = Field(default_factory=list)
    access: list[AccessRule] = Field(default_factory=list)


class AgentSpec(_Model):
    """The environment-independent agent definition. This is what the version hash covers."""

    display_name: Value[str] = Field(default_factory=Value[str].unknown)
    description: Value[str] = Field(default_factory=Value[str].unknown)
    model: ModelSpec = Field(default_factory=ModelSpec)
    instructions: Instructions = Field(default_factory=Instructions)
    tools: list[ToolUse] = Field(default_factory=list)
    knowledge: list[KnowledgeUse] = Field(default_factory=list)
    permissions: Permissions = Field(default_factory=Permissions)
    extensions: dict[str, dict[str, Any]] = Field(default_factory=dict)
    """Platform-specific fields kept intact, keyed by platform."""


class Source(_Model):
    format: str
    """Reader format identifier, such as ``databricks-asset-bundle``."""
    artifact: str
    """Path of the artifact the record was read from, relative to the read root."""
    locator: Optional[str] = None
    """Position of the agent inside the artifact, such as ``resources.apps.support_agent``."""
    detected_by: Optional[str] = None
    """The rule that identified this item as an agent."""
    commit: Optional[str] = None
    pipeline_run: Optional[str] = None


class Quality(_Model):
    completeness: float
    """Weighted share of known fields, from 0 to 1."""
    unknown_fields: list[str] = Field(default_factory=list)


class AgentVersion(_Model):
    """One version of one agent, as read from one source artifact."""

    api_version: Literal["marshal/v0"] = Field(default=API_VERSION, alias="apiVersion")
    kind: Literal["AgentVersion"] = "AgentVersion"
    agent: str
    """Logical agent identity, stable across versions and environments."""
    platform: str
    version: str
    """``sha256:`` content address of the normalized ``spec``."""
    source: Source
    spec: AgentSpec
    bindings: dict[str, Any] = Field(default_factory=dict)
    """Environment-bound values (warehouse IDs, endpoint URLs, environment
    variable names, target overrides). Recorded, but excluded from the version hash
    so the same definition in dev and prod is the same version."""
    quality: Quality


class Agent(_Model):
    """The logical identity that versions belong to."""

    api_version: Literal["marshal/v0"] = Field(default=API_VERSION, alias="apiVersion")
    kind: Literal["Agent"] = "Agent"
    id: str
    platform: str
    display_name: Optional[str] = None
    owner: Value[str] = Field(default_factory=Value[str].unknown)


class DeploymentEvent(str, Enum):
    PROPOSED = "proposed"
    """Captured before deploy, from a pull request."""
    SHIPPED = "shipped"
    """Confirmed after the deploy succeeded."""
    OBSERVED = "observed"
    """Read from the live platform, by a poller or a manual import. When it differs
    from what was last shipped, the agent has drifted."""


class Channel(str, Enum):
    """How an event reached the ledger, which decides how far to trust it."""

    CI = "ci"
    """An authenticated pipeline, through the ingest API."""
    POLLER = "poller"
    """A scheduled read of the platform's own API."""
    MANUAL = "manual"
    """A person ran a collection or imported a file."""


class Deployment(_Model):
    """A version placed into an environment by a pipeline."""

    api_version: Literal["marshal/v0"] = Field(default=API_VERSION, alias="apiVersion")
    kind: Literal["Deployment"] = "Deployment"
    agent: str
    version: str
    platform: str
    environment: str
    event: DeploymentEvent
    at: datetime
    channel: Channel = Channel.MANUAL
    commit: Optional[str] = None
    pipeline_run: Optional[str] = None
