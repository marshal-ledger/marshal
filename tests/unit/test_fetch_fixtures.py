import importlib.util
import io
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def fetcher(monkeypatch):
    spec = importlib.util.spec_from_file_location("fetch_fixtures", ROOT / "scripts" / "fetch_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    return module


def _responses(monkeypatch, fetcher, outcomes):
    calls = []

    def urlopen(request, timeout):
        calls.append(request.full_url)
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return io.BytesIO(outcome)

    monkeypatch.setattr(fetcher.urllib.request, "urlopen", urlopen)
    return calls


def _http(code):
    return urllib.error.HTTPError("https://example.test/f", code, "error", {}, None)


def test_server_errors_and_timeouts_are_retried(fetcher, monkeypatch):
    calls = _responses(monkeypatch, fetcher, [_http(503), TimeoutError(), _http(429), b"body"])
    assert fetcher.download("https://example.test/f") == b"body"
    assert len(calls) == 4


def test_a_missing_file_is_not_retried(fetcher, monkeypatch):
    calls = _responses(monkeypatch, fetcher, [_http(404)])
    with pytest.raises(urllib.error.HTTPError):
        fetcher.download("https://example.test/f")
    assert len(calls) == 1


def test_a_lasting_server_error_stops_a_latest_fetch(fetcher, monkeypatch, tmp_path):
    # Reported as "missing", a 503 made the golden tests fail on files upstream never removed.
    _responses(monkeypatch, fetcher, [_http(503)] * 1000)
    with pytest.raises(urllib.error.HTTPError):
        fetcher.main(["--ref", "HEAD", "--cache", str(tmp_path)])


def test_a_file_removed_upstream_is_reported_at_a_latest_ref(fetcher, monkeypatch, tmp_path, capsys):
    _responses(monkeypatch, fetcher, [_http(404)] + [b"x"] * 1000)
    assert fetcher.main(["--ref", "HEAD", "--cache", str(tmp_path)]) == 0
    assert "missing at HEAD" in capsys.readouterr().err
