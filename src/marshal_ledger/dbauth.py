"""Passwords for Postgres stores that are obtained at connection time.

A password provider is a callable that takes no arguments and returns the
password for the next connection. The store calls it each time it connects or
reconnects, so a provider can return short-lived tokens. A password in the
store URL is used when no provider is configured.

``azure`` signs in to Azure Database for PostgreSQL with a Microsoft Entra ID
access token from ``DefaultAzureCredential`` (managed identity, AKS workload
identity, environment variables or a developer sign-in). It needs the ``azure``
extra.
"""

from __future__ import annotations

import importlib
import threading
import time
from typing import Any, Callable, Optional

PasswordProvider = Callable[[], str]

AZURE_POSTGRES_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"
"""The token scope Azure Database for PostgreSQL accepts in place of a password."""


class AzureTokenProvider:
    """Microsoft Entra ID access tokens for Azure Database for PostgreSQL.

    A token is reused until ``refresh_margin`` seconds before it expires, then
    replaced. Postgres checks the token only when a connection signs in, so an
    open connection outlives its token.
    """

    def __init__(self, credential: Any = None, *, refresh_margin: float = 300, clock: Callable[[], float] = time.time):
        if credential is None:
            try:
                from azure.identity import DefaultAzureCredential
            except ImportError as exc:
                raise ImportError("Entra ID sign-in needs: pip install 'marshal-ledger[azure]'") from exc
            credential = DefaultAzureCredential()
        self._credential = credential
        self._margin = refresh_margin
        self._clock = clock
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._expires_on = 0.0

    def __call__(self) -> str:
        with self._lock:
            if self._token is None or self._clock() >= self._expires_on - self._margin:
                access = self._credential.get_token(AZURE_POSTGRES_SCOPE)
                self._token, self._expires_on = access.token, float(access.expires_on)
            return self._token

    def __repr__(self) -> str:
        return "AzureTokenProvider()"


def password_provider(name: str) -> PasswordProvider:
    """The provider a configuration names: ``azure``, or ``module:attribute`` for a
    callable of your own that returns the password."""
    if name == "azure":
        return AzureTokenProvider()
    module, sep, attribute = name.partition(":")
    if not sep or not module or not attribute:
        raise ValueError(f"unknown store password provider {name!r}: use 'azure' or 'module:callable'")
    provider = importlib.import_module(module)
    for part in attribute.split("."):
        provider = getattr(provider, part)
    if not callable(provider):
        raise ValueError(f"store password provider {name!r} is not callable")
    return provider
