"""The ingest API.

Pipelines send ledger records here, authenticated with their CI provider's OIDC
token. The server verifies the token, applies the ingestion policy, normalizes
the records and writes them to the store. It is the only component that holds
store credentials.

Run with ``marshal serve --config marshal-server.yaml``. Requires the
``server`` extra: ``pip install 'marshal-ledger[server]'``.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Any, Optional

import yaml
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .dbauth import password_provider
from .model import AgentVersion, Channel, DeploymentEvent, Entity
from .oidc import Caller, IssuerConfig, KeyResolver, RepositoryNotAllowed, TokenError, Verifier
from .risk import RiskRules
from .service import ingest
from .store import Actor, OwnershipError, Store, StoreUnavailable
from .web import WebSettings, create_web_app

MAX_RECORDS = 500


class ShippedRule(BaseModel):
    """Who may record what is live in an environment. A request passes if any rule matches."""

    model_config = ConfigDict(extra="forbid")

    refs: list[str] = Field(default_factory=list)
    """Glob patterns for Git refs, such as ``refs/heads/main`` or ``refs/tags/v*``."""
    environments: list[str] = Field(default_factory=list)
    """CI deployment environments, such as a GitHub environment with required reviewers."""
    protected_refs: bool = False
    """Accept any ref the provider reports as protected (GitLab)."""
    protected_environments: bool = False
    """Accept any environment the provider reports as protected (GitLab)."""
    workflows: list[str] = Field(default_factory=list)
    """Glob patterns for the caller's workflow, for providers whose tokens name no ref: a Jenkins
    job URL, such as the main branch's job, or an Azure DevOps subject, such as a service
    connection with a branch control check. Also matches GitHub workflow refs and GitLab CI
    config refs."""


class ShippedPolicy(ShippedRule):
    """Who may mark a version as shipped. The top-level rule applies to every environment that
    ``by_environment`` doesn't name."""

    refs: list[str] = Field(default_factory=lambda: ["refs/heads/main"])
    """Glob patterns for Git refs, such as ``refs/heads/main`` or ``refs/tags/v*``."""
    by_environment: dict[str, ShippedRule] = Field(default_factory=dict)
    """Rules for the ledger environment an event names, such as ``prod``. An environment listed
    here gets only its own rule, not the top-level one; its ``refs`` default to none."""

    def rule_for(self, environment: str | None) -> ShippedRule:
        if environment is not None and environment in self.by_environment:
            return self.by_environment[environment]
        return self


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shipped: ShippedPolicy = Field(default_factory=ShippedPolicy)
    risk: RiskRules = Field(default_factory=RiskRules)
    """The organization's risk rules, added to the built-in ratings. Only the server reads them."""


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store: str
    """A SQLite file path, a postgresql:// URL, or env:VARIABLE to read either from the environment."""
    store_auth: Optional[str] = None
    """How a Postgres store gets its password at each connection: ``azure`` for a Microsoft Entra ID
    access token, or ``module:callable`` for a function of your own. Unset: the password in the URL."""
    issuers: list[IssuerConfig] = Field(min_length=1)
    policy: Policy = Field(default_factory=Policy)
    hash_only: bool = False
    """Store instruction hashes but never instruction text, whatever clients send."""
    web: WebSettings = Field(default_factory=WebSettings)
    """The read-only web view, served behind the organization's SSO proxy."""

    @classmethod
    def load(cls, path: str | Path) -> "ServerConfig":
        config = cls.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
        if config.store.startswith("env:"):
            # Keeps credentials out of the configuration file: store: env:MARSHAL_STORE_URL
            variable = config.store.removeprefix("env:")
            value = os.environ.get(variable)
            if not value:
                raise ValueError(f"environment variable {variable} for the store is not set")
            config.store = value
        return config


class EventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: DeploymentEvent
    environment: str = Field(min_length=1, max_length=200)
    records: list[AgentVersion] = Field(max_length=MAX_RECORDS)
    entities: list[Entity] = Field(default_factory=list)


def may_ship(caller: Caller, policy: ShippedPolicy, environment: str | None = None) -> bool:
    """Whether ``caller`` may record a shipped or observed event for the ledger ``environment``."""
    policy = policy.rule_for(environment)
    if caller.ref and any(fnmatch.fnmatchcase(caller.ref, p) for p in policy.refs):
        return True
    if caller.environment and caller.environment in policy.environments:
        return True
    if policy.protected_refs and caller.ref_protected:
        return True
    if policy.protected_environments and caller.environment_protected:
        return True
    if caller.workflow and any(fnmatch.fnmatchcase(caller.workflow, p) for p in policy.workflows):
        return True
    return False


def store_unavailable(request: Request, exc: Exception) -> JSONResponse:
    # The cause can name hosts, so it stays out of the response.
    return JSONResponse({"detail": "the store is unavailable; retry shortly"}, status_code=503,
                        headers={"Retry-After": "5"})


def create_app(config: ServerConfig, key_resolver: KeyResolver | None = None) -> FastAPI:
    verifier = Verifier(config.issuers, key_resolver)
    password = None
    if config.store_auth:
        if not config.store.startswith(("postgresql://", "postgres://")):
            raise ValueError("store_auth applies only to a postgresql:// store")
        password = password_provider(config.store_auth)
    # No /docs, /redoc or /openapi.json: they would be unauthenticated, and the docs pages load
    # their scripts from a CDN. The API is described in docs/ingest-server.md.
    app = FastAPI(title="Marshal ingest API", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_exception_handler(StoreUnavailable, store_unavailable)

    def caller(authorization: Optional[str] = Header(default=None)) -> Caller:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(401, "a bearer token is required", headers={"WWW-Authenticate": "Bearer"})
        try:
            return verifier.verify(token.strip())
        except TokenError as exc:
            raise HTTPException(401, str(exc), headers={"WWW-Authenticate": "Bearer"}) from exc
        except RepositoryNotAllowed as exc:
            raise HTTPException(403, str(exc)) from exc

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v0/events")
    def post_events(request: EventRequest, who: Caller = Depends(caller)) -> dict[str, Any]:
        # Shipped and observed events both claim what is live, so both need the shipped policy.
        shipped = config.policy.shipped
        if request.event in (DeploymentEvent.SHIPPED, DeploymentEvent.OBSERVED) and not may_ship(
            who, shipped, request.environment
        ):
            rule = f"rule for {request.environment}" if request.environment in shipped.by_environment else "policy"
            raise HTTPException(
                403,
                f"{who.repository} at {who.ref or 'an unknown ref'} may not record {request.event.value} events "
                f"for {request.environment}; the shipped {rule} requires a protected ref, a deployment environment "
                "or a workflow it lists",
            )
        channel = Channel.SCHEDULED if request.event is DeploymentEvent.OBSERVED else Channel.CI
        records = []
        for record in request.records:
            # The token, not the request body, says which commit and run this was.
            source = record.source.model_copy(
                update={
                    "commit": who.sha or record.source.commit,
                    "pipeline_run": f"{who.provider}:{who.run}" if who.run else record.source.pipeline_run,
                }
            )
            records.append(record.model_copy(update={"source": source}))
        actor = Actor(issuer=who.issuer, repository=who.repository, ref=who.ref, workflow=who.workflow)
        with Store(config.store, password=password) as store:
            try:
                reports = ingest(
                    store,
                    records,
                    event=request.event,
                    environment=request.environment,
                    entities=request.entities,
                    actor=actor,
                    hash_only=config.hash_only,
                    channel=channel,
                    risk=config.policy.risk,
                )
            except OwnershipError as exc:
                raise HTTPException(403, f"agent ownership: {exc}") from exc
        return {"reports": [r.to_dict() for r in reports]}

    if config.web.enabled:
        app.mount("/", create_web_app(config.store, config.web, password=password, risk=config.policy.risk))
    return app
