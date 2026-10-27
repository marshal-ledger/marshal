"""Verification of CI OIDC tokens.

A pipeline proves who it is with a short-lived token signed by its CI provider.
The verifier checks the signature against the issuer's published keys, the
issuer, the audience and the token's lifetime, then maps the provider's claims
to a :class:`Caller`. Only configured issuers are accepted, and only from
callers that match the issuer's allowlist: a provider such as GitHub signs
tokens for every repository it hosts, so the issuer alone proves nothing about
the organization.

Each provider names the caller with one claim, the identity the allowlist
matches: the repository for GitHub and GitLab, the job (``sub``, the job URL by
default) for Jenkins, and the pipeline or service connection (``sub``) for
Azure DevOps. A token without that claim is refused. For Jenkins and Azure
DevOps, ``owner: parent`` makes the identity the item that contains the caller
instead: the multibranch project or folder that holds the job, or the Azure
DevOps project.
"""

from __future__ import annotations

import fnmatch
import json
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Literal, Optional

import jwt
from pydantic import BaseModel, ConfigDict, Field, model_validator

ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"]
LEEWAY_SECONDS = 30


class TokenError(Exception):
    """The token is missing, malformed, untrusted or expired. Maps to HTTP 401."""


class RepositoryNotAllowed(Exception):
    """The token is valid but its caller is not on the issuer's allowlist. Maps to HTTP 403."""


Provider = Literal["github", "gitlab", "jenkins", "azure-devops"]

DEFAULT_AUDIENCES = {"azure-devops": "api://AzureADTokenExchange"}
"""Providers whose audience is fixed. Azure DevOps sets it; a pipeline cannot choose another."""

IDENTITY_CLAIMS = {"github": "repository", "gitlab": "project_path", "jenkins": "sub", "azure-devops": "sub"}


class IssuerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issuer: str
    """The token's ``iss`` value, such as ``https://token.actions.githubusercontent.com``."""
    provider: Provider
    audience: str = ""
    """Defaults to ``marshal``, or to the fixed audience of a provider that sets one."""
    repositories: list[str] = Field(min_length=1)
    """Glob patterns for callers allowed to write: repositories such as ``my-org/*``, Jenkins
    job URLs or Azure DevOps subjects."""
    jwks_uri: Optional[str] = None
    """Where to fetch signing keys. Discovered from the issuer when not set."""
    owner: Literal["caller", "parent"] = "caller"
    """Jenkins and Azure DevOps: ``parent`` identifies callers by the job's parent (a multibranch
    project or folder) or by the Azure DevOps project, so that every branch's job, or every
    pipeline and service connection in a project, records the same agents."""

    @model_validator(mode="after")
    def _defaults(self) -> "IssuerConfig":
        if not self.audience:
            self.audience = DEFAULT_AUDIENCES.get(self.provider, "marshal")
        if self.owner == "parent" and self.provider not in _PARENTS:
            raise ValueError(f"owner: parent applies to {' and '.join(_PARENTS)} issuers, not {self.provider}")
        return self


@dataclass(frozen=True)
class Caller:
    """The pipeline that presented a valid token."""

    issuer: str
    provider: str
    repository: str
    """The identity the allowlist matched and agents are bound to: a repository, a Jenkins job or
    an Azure DevOps subject, or their parent with ``owner: parent``."""
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


def _text(value: Any) -> Optional[str]:
    return None if value is None else str(value)


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


def _jenkins(claims: dict[str, Any]) -> dict[str, Any]:
    # The OpenID Connect Provider plugin sets sub (the job URL) and build_number by default.
    # ref and sha are present only if an administrator adds claim templates for them.
    return dict(
        repository=claims.get("sub"),
        ref=_text(claims.get("ref")),
        ref_protected=None,
        environment=None,
        environment_protected=None,
        workflow=claims.get("sub"),
        sha=_text(claims.get("sha")),
        run=_text(claims.get("build_number")),
        event_name=None,
    )


def _azure_devops(claims: dict[str, Any]) -> dict[str, Any]:
    # sub is p://<org>/<project>/<pipeline> or sc://<org>/<project>/<service connection>.
    # The token carries no branch, commit or run.
    return dict(
        repository=claims.get("sub"),
        ref=None,
        ref_protected=None,
        environment=None,
        environment_protected=None,
        workflow=claims.get("sub"),
        sha=None,
        run=None,
        event_name=None,
    )


_PROVIDERS = {"github": _github, "gitlab": _gitlab, "jenkins": _jenkins, "azure-devops": _azure_devops}


def _jenkins_parent(sub: str) -> Optional[str]:
    # A job URL ends in /job/<name>/; its parent is the URL without that last segment.
    head, separator, name = sub.rstrip("/").rpartition("/job/")
    return head + "/" if separator and name and "/job/" in head else None


def _azure_devops_parent(sub: str) -> Optional[str]:
    # p://<org>/<project>/<pipeline> and sc://<org>/<project>/<connection> share <org>/<project>.
    scheme, separator, path = sub.partition("://")
    parts = path.split("/")
    return "/".join(parts[:2]) if separator and scheme in ("p", "sc") and len(parts) == 3 and all(parts) else None


_PARENTS = {"jenkins": _jenkins_parent, "azure-devops": _azure_devops_parent}

KeyResolver = Callable[[IssuerConfig, str], Any]
"""Returns the verification key for a token. Replaceable in tests."""


def _fetch_json(url: str) -> Any:
    # A new opener per fetch reads HTTPS_PROXY, HTTP_PROXY and NO_PROXY now, not at the first request
    # the process made. Certificates are checked against OpenSSL's default store, which SSL_CERT_FILE
    # and SSL_CERT_DIR replace.
    with urllib.request.build_opener().open(url, timeout=10) as response:
        return json.load(response)


class _JwksResolver:
    """Fetches and caches each issuer's signing keys, by key id."""

    MAX_AGE = 3600
    """Seconds before keys are fetched again."""
    MISS_INTERVAL = 60
    """Seconds between fetches for an unknown key id, so forged key ids can't cause a fetch per request."""

    def __init__(self) -> None:
        self._jwks_uris: dict[str, str] = {}
        self._keys: dict[str, tuple[float, dict[Optional[str], Any]]] = {}

    def _jwks_uri(self, issuer: IssuerConfig) -> str:
        if issuer.jwks_uri:
            return issuer.jwks_uri
        if issuer.issuer not in self._jwks_uris:
            discovery = _fetch_json(issuer.issuer.rstrip("/") + "/.well-known/openid-configuration")
            self._jwks_uris[issuer.issuer] = discovery["jwks_uri"]
        return self._jwks_uris[issuer.issuer]

    def _fetch(self, issuer: IssuerConfig) -> dict[Optional[str], Any]:
        keys = jwt.PyJWKSet.from_dict(_fetch_json(self._jwks_uri(issuer))).keys
        return {key.key_id: key.key for key in keys if key.public_key_use in ("sig", None)}

    def __call__(self, issuer: IssuerConfig, token: str) -> Any:
        kid = jwt.get_unverified_header(token).get("kid")
        cached = self._keys.get(issuer.issuer)
        if cached is None:
            stale = True
        else:
            age, keys = time.monotonic() - cached[0], cached[1]
            stale = age > self.MAX_AGE or (kid not in keys and age > self.MISS_INTERVAL)
        if stale:
            keys = self._fetch(issuer)
            self._keys[issuer.issuer] = (time.monotonic(), keys)
        if kid not in keys:
            raise jwt.InvalidTokenError(f"issuer publishes no signing key {kid!r}")
        return keys[kid]


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
        if not repository or not isinstance(repository, str):
            raise TokenError(f"token has no {IDENTITY_CLAIMS[issuer.provider]} claim")
        if issuer.owner == "parent":
            parent = _PARENTS[issuer.provider](repository)
            if parent is None:
                raise TokenError(f"cannot tell the parent of {repository!r}")
            fields["repository"] = repository = parent
        if not any(fnmatch.fnmatchcase(repository, pattern) for pattern in issuer.repositories):
            raise RepositoryNotAllowed(f"{repository!r} is not allowed for issuer {issuer.issuer}")
        return Caller(issuer=issuer.issuer, provider=issuer.provider, **fields)
