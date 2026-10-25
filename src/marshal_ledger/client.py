"""Client for the ingest API, used by ``marshal push`` and the GitHub Action.

Uses only the standard library, so pipelines need nothing beyond the base
package. Tokens are read from the CI environment at run time and are never
printed or written to disk.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable

from .model import AgentVersion, DeploymentEvent, Entity
from .service import AgentReport


class ClientError(Exception):
    pass


def ci_token(audience: str, token_env: str | None = None) -> str:
    """Obtain an OIDC token for ``audience`` from the CI environment.

    With ``token_env``, read the token from that environment variable (GitLab
    ``id_tokens``, or any provider that injects one). Otherwise request one from
    GitHub Actions, which requires the job permission ``id-token: write``.
    """
    if token_env:
        token = os.environ.get(token_env)
        if not token:
            raise ClientError(f"environment variable {token_env} is empty or not set")
        return token
    url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL")
    request_token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not url or not request_token:
        raise ClientError(
            "no OIDC token available: in GitHub Actions grant the job `id-token: write`; "
            "elsewhere pass --token-env with the variable that holds the token"
        )
    separator = "&" if "?" in url else "?"
    request = urllib.request.Request(
        f"{url}{separator}audience={urllib.parse.quote(audience, safe='')}",
        headers={"Authorization": f"Bearer {request_token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)["value"]
    except (urllib.error.URLError, KeyError, ValueError) as exc:
        raise ClientError(f"could not obtain a GitHub OIDC token: {exc}") from exc


def push(
    url: str,
    token: str,
    records: Iterable[AgentVersion],
    *,
    event: DeploymentEvent,
    environment: str,
    entities: Iterable[Entity] = (),
) -> list[AgentReport]:
    body: dict[str, Any] = {
        "event": event.value,
        "environment": environment,
        "records": [r.model_dump(mode="json", by_alias=True) for r in records],
        "entities": [e.model_dump(mode="json") for e in entities],
    }
    request = urllib.request.Request(
        url.rstrip("/") + "/v0/events",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            detail = json.load(exc).get("detail")
        except ValueError:
            detail = None
        raise ClientError(f"ingest API returned {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise ClientError(f"could not reach {url}: {exc.reason}") from exc
    return [AgentReport.from_dict(r) for r in payload["reports"]]
