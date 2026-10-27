import time

import pytest

from conftest import AZURE_DEVOPS, GITHUB, GITLAB, JENKINS
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


@pytest.fixture
def ci_verifier(signer):
    return Verifier(
        [
            IssuerConfig(issuer=JENKINS, provider="jenkins", repositories=["https://jenkins.example.com/job/agents/*"]),
            IssuerConfig(issuer=AZURE_DEVOPS, provider="azure-devops", repositories=["sc://acme/agents/*", "p://acme/agents/*"]),
        ],
        key_resolver=signer.resolver,
    )


def test_jenkins_token(ci_verifier, signer):
    caller = ci_verifier.verify(signer.jenkins())
    assert caller.provider == "jenkins"
    assert caller.repository == "https://jenkins.example.com/job/agents/job/finance/job/main/"
    assert (caller.run, caller.ref, caller.sha) == ("42", None, None)


def test_jenkins_claim_templates_supply_ref_and_commit(ci_verifier, signer):
    caller = ci_verifier.verify(signer.jenkins(ref="refs/heads/main", sha="3f9c2e1"))
    assert (caller.ref, caller.sha) == ("refs/heads/main", "3f9c2e1")


def test_azure_devops_token(ci_verifier, signer):
    caller = ci_verifier.verify(signer.azure_devops())
    assert (caller.provider, caller.repository) == ("azure-devops", "sc://acme/agents/marshal-ingest")
    assert (caller.ref, caller.sha, caller.run) == (None, None, None)
    pipeline = ci_verifier.verify(signer.azure_devops(sub="p://acme/agents/record-agents"))
    assert pipeline.repository == "p://acme/agents/record-agents"


def test_azure_devops_audience_is_fixed_by_default():
    assert IssuerConfig(issuer=AZURE_DEVOPS, provider="azure-devops", repositories=["*"]).audience == (
        "api://AzureADTokenExchange"
    )
    assert IssuerConfig(issuer=JENKINS, provider="jenkins", repositories=["*"]).audience == "marshal"


@pytest.mark.parametrize("provider", ["jenkins", "azure_devops"])
def test_wrong_audience_is_refused(ci_verifier, signer, provider):
    with pytest.raises(TokenError):
        ci_verifier.verify(getattr(signer, provider)(aud="something-else"))


@pytest.mark.parametrize(
    "provider, sub",
    [
        ("jenkins", "https://jenkins.example.com/job/other-team/job/agents/"),
        ("jenkins", "https://jenkins.evil.example/job/agents/job/finance/"),
        ("azure_devops", "sc://acme/other-project/marshal-ingest"),
        ("azure_devops", "sc://evil/agents/marshal-ingest"),
    ],
)
def test_caller_outside_allowlist_is_refused(ci_verifier, signer, provider, sub):
    with pytest.raises(RepositoryNotAllowed):
        ci_verifier.verify(getattr(signer, provider)(sub=sub))


@pytest.mark.parametrize("provider", ["jenkins", "azure_devops"])
def test_token_without_sub_is_refused(ci_verifier, signer, provider):
    with pytest.raises(TokenError, match="no sub claim"):
        ci_verifier.verify(getattr(signer, provider)(sub=None))


def test_token_from_one_issuer_does_not_pass_as_another(ci_verifier, signer):
    # A GitHub token is not trusted by a verifier that only knows Jenkins and Azure DevOps.
    with pytest.raises(TokenError):
        ci_verifier.verify(signer.github())
    with pytest.raises(TokenError):
        ci_verifier.verify(signer.jenkins(iss=AZURE_DEVOPS))


def test_owner_parent_names_the_multibranch_project_or_azure_devops_project(signer):
    verifier = Verifier(
        [
            IssuerConfig(issuer=JENKINS, provider="jenkins", owner="parent", repositories=["https://jenkins.example.com/job/agents/*"]),
            IssuerConfig(issuer=AZURE_DEVOPS, provider="azure-devops", owner="parent", repositories=["acme/agents"]),
        ],
        key_resolver=signer.resolver,
    )
    job = verifier.verify(signer.jenkins())
    assert job.repository == "https://jenkins.example.com/job/agents/job/finance/"
    assert job.workflow == "https://jenkins.example.com/job/agents/job/finance/job/main/"
    assert verifier.verify(signer.azure_devops(sub="p://acme/agents/record-agents")).repository == "acme/agents"
    assert verifier.verify(signer.azure_devops()).workflow == "sc://acme/agents/marshal-ingest"
    # A subject whose parent can't be read is refused, not guessed at.
    with pytest.raises(TokenError, match="parent"):
        verifier.verify(signer.jenkins(sub="https://jenkins.example.com/job/top-level/"))
    with pytest.raises(TokenError, match="parent"):
        verifier.verify(signer.azure_devops(sub="sc://acme/agents/nested/name"))
    with pytest.raises(RepositoryNotAllowed):
        verifier.verify(signer.azure_devops(sub="p://acme/agents-sandbox/record-agents"))


def test_owner_parent_is_only_for_jenkins_and_azure_devops():
    with pytest.raises(ValueError, match="owner: parent"):
        IssuerConfig(issuer=GITHUB, provider="github", owner="parent", repositories=["acme/*"])
