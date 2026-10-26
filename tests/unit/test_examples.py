"""Example workflows and the GitHub Action stay well-formed."""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted((ROOT / "examples").rglob("*.yml")) + sorted((ROOT / ".github" / "workflows").glob("*.yml"))
SCHEDULED = sorted((ROOT / "examples" / "scheduled").glob("*.yml"))


def steps(document):
    if "runs" in document:
        yield from document["runs"]["steps"]
    for job in (document.get("jobs") or {}).values():
        yield from job.get("steps") or []


@pytest.mark.parametrize("path", WORKFLOWS + [ROOT / "collect" / "action.yml"], ids=lambda p: p.relative_to(ROOT).as_posix())
def test_run_steps_take_values_from_the_environment(path):
    """Expressions in a `run:` script are a script-injection risk; pass them through `env:` instead."""
    for step in steps(yaml.safe_load(path.read_text())):
        assert "${{" not in step.get("run", ""), f"{step.get('name')}: use env: rather than an expression in run:"


@pytest.mark.parametrize("path", SCHEDULED, ids=lambda p: p.name)
def test_scheduled_workflows_push_observed_events(path):
    document = yaml.safe_load(path.read_text())
    triggers = document[True] if True in document else document["on"]  # YAML 1.1 reads `on` as a boolean
    assert "schedule" in triggers and "workflow_dispatch" in triggers
    assert document["permissions"]["id-token"] == "write"
    [collect] = [s for s in steps(document) if str(s.get("uses", "")).startswith("marshal-ledger/marshal/collect@")]
    assert collect["with"]["event"] == "observed"
    assert collect["with"]["environment"]


def test_scheduled_examples_are_documented():
    readme = (ROOT / "examples" / "scheduled" / "README.md").read_text()
    for path in SCHEDULED:
        assert f"]({path.name})" in readme
