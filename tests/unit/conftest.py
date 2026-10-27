import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

GITHUB = "https://token.actions.githubusercontent.com"
GITLAB = "https://gitlab.example.com"
JENKINS = "https://jenkins.example.com/oidc"
AZURE_DEVOPS = "https://vstoken.dev.azure.com/0d6c2f3e-8b1a-4c5e-9f7d-2a4b6c8e0f13"


class Signer:
    """Issues CI-style OIDC tokens signed with a throwaway key."""

    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public_key = self.key.public_key()

    def github(self, repository="acme/finance-agents", ref="refs/heads/main", **claims):
        now = int(time.time())
        payload = {
            "iss": GITHUB,
            "aud": "marshal",
            "iat": now,
            "nbf": now,
            "exp": now + 300,
            "repository": repository,
            "ref": ref,
            "workflow_ref": f"{repository}/.github/workflows/deploy.yml@{ref}",
            "run_id": "8812",
            "run_attempt": "1",
            "event_name": "push",
            "sha": "3f9c2e1",
        }
        payload.update(claims)
        return jwt.encode(payload, self.key, algorithm="RS256")

    def gitlab(self, project_path="acme/hr-agents", ref="main", **claims):
        now = int(time.time())
        payload = {
            "iss": GITLAB,
            "aud": "marshal",
            "iat": now,
            "exp": now + 300,
            "project_path": project_path,
            "ref": ref,
            "ref_type": "branch",
            "ref_protected": "true",
            "pipeline_id": "1204",
            "pipeline_source": "push",
            "sha": "a1b2c3d",
        }
        payload.update(claims)
        return jwt.encode(payload, self.key, algorithm="RS256")

    def jenkins(self, sub="https://jenkins.example.com/job/agents/job/finance/job/main/", **claims):
        # The OpenID Connect Provider plugin's default build claims are sub (the job URL) and build_number.
        now = int(time.time())
        payload = {"iss": JENKINS, "aud": "marshal", "iat": now, "exp": now + 3600, "sub": sub, "build_number": 42}
        payload.update(claims)
        payload = {name: value for name, value in payload.items() if value is not None}
        return jwt.encode(payload, self.key, algorithm="RS256", headers={"kid": "marshal-id-token"})

    def azure_devops(self, sub="sc://acme/agents/marshal-ingest", **claims):
        now = int(time.time())
        payload = {
            "iss": AZURE_DEVOPS,
            "aud": "api://AzureADTokenExchange",
            "iat": now,
            "nbf": now,
            "exp": now + 600,
            "jti": "4f2a9c1e-7b3d-4e8f-a6c5-1d2e3f4a5b6c",
            "sub": sub,
        }
        payload.update(claims)
        payload = {name: value for name, value in payload.items() if value is not None}
        return jwt.encode(payload, self.key, algorithm="RS256", headers={"kid": "ED1C1ACAF8970EA8"})

    def resolver(self, issuer, token):
        return self.public_key


@pytest.fixture(scope="session")
def signer():
    return Signer()


@pytest.fixture
def ledger_location(tmp_path):
    """A fresh store location: SQLite by default, or Postgres when MARSHAL_TEST_POSTGRES_URL is set."""
    import os

    url = os.environ.get("MARSHAL_TEST_POSTGRES_URL")
    if not url:
        return str(tmp_path / "ledger.sqlite")
    import psycopg

    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute("DROP SCHEMA public CASCADE")
        connection.execute("CREATE SCHEMA public")
    return url
