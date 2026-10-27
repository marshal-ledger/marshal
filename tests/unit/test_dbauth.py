import sys
import types
from collections import namedtuple

import pytest

from marshal_ledger.dbauth import AZURE_POSTGRES_SCOPE, AzureTokenProvider, password_provider

AccessToken = namedtuple("AccessToken", "token expires_on")


class FakeCredential:
    """Stands in for azure-identity's DefaultAzureCredential: tokens that last an hour."""

    def __init__(self, clock):
        self.clock = clock
        self.scopes = []

    def get_token(self, *scopes):
        self.scopes.append(scopes)
        return AccessToken(f"token-{len(self.scopes)}", int(self.clock()) + 3600)


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def test_azure_tokens_are_reused_until_shortly_before_they_expire():
    clock = Clock()
    credential = FakeCredential(clock)
    provider = AzureTokenProvider(credential, clock=clock)
    assert provider() == "token-1"
    clock.now += 3600 - 301
    assert provider() == "token-1"
    clock.now += 2
    assert provider() == "token-2"
    assert credential.scopes == [(AZURE_POSTGRES_SCOPE,), (AZURE_POSTGRES_SCOPE,)]
    assert AZURE_POSTGRES_SCOPE == "https://ossrdbms-aad.database.windows.net/.default"


def test_the_token_is_not_in_the_providers_repr():
    provider = AzureTokenProvider(FakeCredential(Clock()))
    token = provider()
    assert token not in repr(provider) and token not in str(provider)


def test_azure_uses_default_azure_credential(monkeypatch):
    identity = types.ModuleType("azure.identity")
    identity.DefaultAzureCredential = lambda: FakeCredential(Clock())
    monkeypatch.setitem(sys.modules, "azure", types.ModuleType("azure"))
    monkeypatch.setitem(sys.modules, "azure.identity", identity)
    provider = password_provider("azure")
    assert isinstance(provider, AzureTokenProvider)
    assert provider() == "token-1"


def test_azure_without_the_extra_says_what_to_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "azure.identity", None)
    with pytest.raises(ImportError, match=r"marshal-ledger\[azure\]"):
        password_provider("azure")


def test_a_provider_of_your_own(monkeypatch):
    module = types.ModuleType("acme_vault")
    module.secrets = types.SimpleNamespace(postgres=lambda: "from-vault", name="not callable")
    monkeypatch.setitem(sys.modules, "acme_vault", module)
    assert password_provider("acme_vault:secrets.postgres")() == "from-vault"
    with pytest.raises(ValueError, match="not callable"):
        password_provider("acme_vault:secrets.name")


@pytest.mark.parametrize("name", ["vault", "acme_vault:", ":secrets"])
def test_unknown_provider_names_are_refused(name):
    with pytest.raises(ValueError, match="unknown store password provider"):
        password_provider(name)
