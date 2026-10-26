import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def checker(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("check_upstream", ROOT / "scripts" / "check_upstream.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    watch = tmp_path / "watch.json"
    watch.write_text(json.dumps({"sources": [{"id": "fmt", "name": "Format", "extractor": "fake", "docs": "docs/formats/x.md"}]}))
    monkeypatch.setattr(module, "WATCH", watch)
    monkeypatch.setattr(module, "SNAPSHOTS", tmp_path / "snapshots")
    calls = []
    monkeypatch.setattr(module, "_sync_issue", lambda title, body: calls.append((title, body)))
    module.calls = calls
    return module


def test_reports_added_and_removed_names_and_manages_issues(checker, tmp_path):
    checker.EXTRACTORS["fake"] = lambda source: ["Agent.name", "Agent.tools"]
    assert checker.main(["--update"]) == 0
    assert json.loads((tmp_path / "snapshots" / "fmt.json").read_text()) == ["Agent.name", "Agent.tools"]

    checker.EXTRACTORS["fake"] = lambda source: ["Agent.name", "Agent.toolboxes"]
    report = tmp_path / "report.json"
    checker.main(["--issues", "--report", str(report)])
    assert json.loads(report.read_text())["fmt"] == {"added": ["Agent.toolboxes"], "removed": ["Agent.tools"]}
    [(title, body)] = checker.calls
    assert title == "Upstream change: Format"
    assert "`Agent.toolboxes`" in body and "docs/formats/x.md" in body

    checker.calls.clear()
    checker.EXTRACTORS["fake"] = lambda source: ["Agent.name", "Agent.tools"]
    checker.main(["--issues"])
    assert checker.calls == [("Upstream change: Format", None)], "an issue closes when upstream matches again"


def test_golden_failures_become_an_issue(checker, tmp_path):
    checker.EXTRACTORS["fake"] = lambda source: []
    output = tmp_path / "golden.txt"
    output.write_text("FAILED tests/golden/test_translation.py::test_x - AssertionError\n1 failed, 3 passed\n")
    checker.main(["--issues", "--only", "none", "--golden-result", str(output)])
    [(title, body)] = checker.calls
    assert "golden tests fail" in title and "test_translation.py::test_x" in body


def test_extractor_errors_are_reported_not_raised(checker, tmp_path):
    def broken(source):
        raise OSError("network down")

    checker.EXTRACTORS["fake"] = broken
    report = tmp_path / "report.json"
    assert checker.main(["--report", str(report)]) == 0
    assert "network down" in json.loads(report.read_text())["fmt"]["error"]
