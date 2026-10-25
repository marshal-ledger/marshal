"""Verification of CI OIDC tokens.

A pipeline proves who it is with a short-lived token signed by its CI provider.
The verifier checks the signature against the issuer's published keys, the
issuer, the audience and the token's lifetime, then maps the provider's claims
to a :class:`Caller`. Only configured issuers are accepted, and only from
repositories that match the issuer's allowlist: a provider such as GitHub
signs tokens for every repository it hosts, so the issuer alone proves nothing
about the organization.
"""

from __future__ import annotations

import fnmatch
import json
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Literal, Optional

import jwt
from pydantic import BaseModel, ConfigDict, Field

ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"]
LEEWAY_SECONDS = 30


class TokenError(Exception):
    """The token is missing, malformed, untrusted or expired. Maps to HTTP 401."""


class RepositoryNotAllowed(Exception):
    """The token is valid but its repository is not on the issuer's allowlist. Maps to HTTP 403."""


class IssuerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issuer: str
    """The token's ``iss`` value, such as ``https://token.actions.githubusercontent.com``."""
    provider: Literal["github", "gitlab"]
    audience: str = "marshal"
    repositories: list[str] = Field(min_length=1)
    """Glob patterns for repositories allowed to write, such as ``my-org/*``."""
    jwks_uri: Optional[str] = None
    """Where to fetch signing keys. Discovered from the issuer when not set."""


@dataclass(frozen=True)
class Caller:
    """The pipeline that presented a valid token."""

    issuer: str
    provider: str
    repository: str
    ref: Optional[str]
    """A full Git ref, such as ``refs/heads/main``."""
    ref_protected: Optional[bool]
    environment: Optional[str]
    environment_protected: Optional[bool]
    workflow: Optional[str]
    sha: Optional[str]
    run: Optional[str]
    event_name: Optional[str]


def _bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    return str(value).lower() == "true"


def _github(claims: dict[str, Any]) -> dict[str, Any]:
    run = claims.get("run_id")
    if run and claims.get("run_attempt"):
        run = f"{run}/{claims['run_attempt']}"
    return dict(
        repository=claims.get("repository"),
        ref=claims.get("ref"),
        ref_protected=None,
        environment=claims.get("environment"),
        environment_protected=None,
        workflow=claims.get("job_workflow_ref") or claims.get("workflow_ref"),
        sha=claims.get("sha"),
        run=str(run) if run else None,
        event_name=claims.get("event_name"),
    )


def _gitlab(claims: dict[str, Any]) -> dict[str, Any]:
    ref = claims.get("ref")
    if ref and not ref.startswith("refs/"):
        ref = f"refs/tags/{ref}" if claims.get("ref_type") == "tag" else f"refs/heads/{ref}"
    return dict(
        repository=claims.get("project_path"),
        ref=ref,
        ref_protected=_bool(claims.get("ref_protected")),
        environment=claims.get("environment"),
        environment_protected=_bool(claims.get("environment_protected")),
        workflow=claims.get("ci_config_ref_uri"),
        sha=claims.get("sha"),
        run=str(claims["pipeline_id"]) if claims.get("pipeline_id") else None,
        event_name=claims.get("pipeline_source"),
    )


_PROVIDERS = {"github": _github, "gitlab": _gitlab}

KeyResolver = Callable[[IssuerConfig, str], Any]
"""Returns the verification key for a token. Replaceable in tests."""


class _JwksResolver:
    def __init__(self) -> None:
        self._clients: dict[str, jwt.PyJWKClient] = {}

    def _jwks_uri(self, issuer: IssuerConfig) -> str:
        if issuer.jwks_uri:
            return issuer.jwks_uri
        url = issuer.issuer.rstrip("/") + "/.well-known/openid-configuration"
        with urllib.request.urlopen(url, timeout=10) as response:
            return json.load(response)["jwks_uri"]

    def __call__(self, issuer: IssuerConfig, token: str) -> Any:
        client = self._clients.get(issuer.issuer)
        if client is None:
            client = jwt.PyJWKClient(self._jwks_uri(issuer), cache_keys=True, lifespan=3600)
            self._clients[issuer.issuer] = client
        return client.get_signing_key_from_jwt(token).key


class Verifier:
    def __init__(self, issuers: list[IssuerConfig], key_resolver: KeyResolver | None = None):
        self._issuers = {i.issuer: i for i in issuers}
        self._keys = key_resolver or _JwksResolver()

    def verify(self, token: str) -> Caller:
        try:
            unverified = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError as exc:
            raise TokenError(f"malformed token: {exc}") from exc
        issuer = self._issuers.get(str(unverified.get("iss")))
        if issuer is None:
            raise TokenError(f"untrusted issuer {unverified.get('iss')!r}")
        try:
            key = self._keys(issuer, token)
            claims = jwt.decode(
                token,
                key,
                algorithms=ALGORITHMS,
                audience=issuer.audience,
                issuer=issuer.issuer,
                leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "iss", "aud"]},
            )
        except (jwt.PyJWTError, OSError, KeyError, ValueError) as exc:
            raise TokenError(f"invalid token: {exc}") from exc

        fields = _PROVIDERS[issuer.provider](claims)
        repository = fields["repository"]
        if not repository:
            raise TokenError("token names no repository")
        if not any(fnmatch.fnmatchcase(repository, pattern) for pattern in issuer.repositories):
            raise RepositoryNotAllowed(f"repository {repository!r} is not allowed for issuer {issuer.issuer}")
        return Caller(issuer=issuer.issuer, provider=issuer.provider, **fields)
