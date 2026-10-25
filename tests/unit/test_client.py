import io
import json
import urllib.request

import pytest

from marshal_ledger.client import ClientError, ci_token


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_token_from_environment_variable(monkeypatch):
    monkeypatch.setenv("MARSHAL_ID_TOKEN", "abc")
    assert ci_token("marshal", "MARSHAL_ID_TOKEN") == "abc"
    monkeypatch.delenv("MARSHAL_ID_TOKEN")
    with pytest.raises(ClientError):
        ci_token("marshal", "MARSHAL_ID_TOKEN")


def test_token_from_github_actions(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        return FakeResponse(json.dumps({"value": "oidc-token"}).encode())

    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", "https://actions.example/token?api-version=2.0")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "request-token")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert ci_token("marshal") == "oidc-token"
    assert seen["url"] == "https://actions.example/token?api-version=2.0&audience=marshal"
    assert seen["auth"] == "Bearer request-token"


def test_no_token_source_explains_permissions(monkeypatch):
    monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_URL", raising=False)
    monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", raising=False)
    with pytest.raises(ClientError, match="id-token: write"):
        ci_token("marshal")
