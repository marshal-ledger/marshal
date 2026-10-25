import time

import pytest

from conftest import GITHUB, GITLAB
from marshal_ledger.oidc import IssuerConfig, RepositoryNotAllowed, TokenError, Verifier


@pytest.fixture
def verifier(signer):
    return Verifier(
        [
            IssuerConfig(issuer=GITHUB, provider="github", repositories=["acme/*"]),
            IssuerConfig(issuer=GITLAB, provider="gitlab", repositories=["acme/*"]),
        ],
        key_resolver=signer.resolver,
    )


def test_github_token(verifier, signer):
    caller = verifier.verify(signer.github(environment="prod"))
    assert caller.repository == "acme/finance-agents"
    assert caller.ref == "refs/heads/main"
    assert caller.environment == "prod"
    assert caller.run == "8812/1"
    assert caller.workflow.endswith("deploy.yml@refs/heads/main")


def test_gitlab_token_normalizes_ref(verifier, signer):
    caller = verifier.verify(signer.gitlab())
    assert (caller.repository, caller.ref, caller.ref_protected) == ("acme/hr-agents", "refs/heads/main", True)
    tag = verifier.verify(signer.gitlab(ref="v1.2", ref_type="tag", ref_protected="false"))
    assert (tag.ref, tag.ref_protected) == ("refs/tags/v1.2", False)


def test_repository_outside_allowlist_is_refused(verifier, signer):
    with pytest.raises(RepositoryNotAllowed):
        verifier.verify(signer.github(repository="someone-else/agents"))


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "something-else"},
        {"exp": int(time.time()) - 3600},
        {"iss": "https://untrusted.example.com"},
    ],
)
def test_bad_claims_are_refused(verifier, signer, claims):
    with pytest.raises(TokenError):
        verifier.verify(signer.github(**claims))


def test_wrong_signing_key_is_refused(signer):
    from conftest import Signer

    other = Signer()
    verifier = Verifier(
        [IssuerConfig(issuer=GITHUB, provider="github", repositories=["acme/*"])], key_resolver=signer.resolver
    )
    with pytest.raises(TokenError):
        verifier.verify(other.github())


def test_garbage_is_refused(verifier):
    with pytest.raises(TokenError):
        verifier.verify("not-a-token")
