"""Round-trip and cross-format tests against real, public agent definitions.

- Every open-format fixture must survive read → write → read in its own format
  with no loss.
- Every fixture, in every format, must convert to every other format with each
  lost field explained by a declared loss or a writer note.
- Written ADL and Agent Spec documents must validate against the published
  schemas.
"""

import json
from pathlib import Path

import jsonschema
import pytest
from jsonschema.exceptions import best_match

from marshal_ledger.convert import convert
from marshal_ledger.readers import READERS
from marshal_ledger.writers import WRITERS

CACHE = Path(__file__).resolve().parent.parent / "fixtures" / ".cache"

SOURCES = [
    ("adl", "adl/examples"),
    ("agent-spec", "agent-spec/examples"),
    ("oasf", "oasf/records"),
    ("databricks-asset-bundle", "databricks/genie-space-nyc-taxi"),
    ("databricks-asset-bundle", "databricks/app-with-genie-space"),
    ("databricks-asset-bundle", "databricks/agent-openai-agents-sdk"),
    ("databricks-asset-bundle", "databricks/agent-langgraph-advanced"),
    ("databricks-asset-bundle", "databricks/agent-langgraph"),
    ("python", "python/openai-agents"),
    ("python", "python/google-adk"),
    ("python", "python/crewai-template"),
    ("python", "python/anthropic-quickstarts"),
    ("cloudformation", "cloudformation/bedrock-agents"),
    ("cloudformation", "cloudformation/serverless-agents"),
    ("cloudformation", "cloudformation/multi-agent"),
    ("cloudformation", "cloudformation/agentcore"),
    ("copilot-studio", "copilot-studio/all-the-things"),
    ("copilot-studio", "copilot-studio/mcp-agent"),
    ("agentforce", "agentforce/agent-script"),
]
# Formats whose writer reproduces the whole source, so a native round trip must be lossless.
# A CloudFormation template also defines the agent's IAM roles and executors, which the
# writer references rather than generates; its round trip is held to "every loss explained".
FULL = {"adl", "agent-spec", "oasf", "agentforce"}


def records(fmt, fixture):
    path = CACHE / fixture
    if not path.exists():
        pytest.skip(f"fixture {fixture} not fetched; run python scripts/fetch_fixtures.py")
    if fixture == "python/crewai-template":
        # The official template is rendered by `crewai create crew`; do the same substitution.
        crew = path / "crew.py"
        crew.write_text(crew.read_text().replace("{{crew_name}}", "ResearchCrew"))
    result = READERS[fmt]().read(path)
    assert result.records, f"no agents read from {fixture}"
    return result.records


def schema_validator(fixture, file, ref=None):
    path = CACHE / fixture / file
    if not path.exists():
        pytest.skip(f"schema {fixture} not fetched")
    schema = json.loads(path.read_text())
    if ref:
        schema = {"$ref": ref, "$defs": schema["$defs"]}
    return jsonschema.Draft202012Validator(schema)


def validate_copilot_studio(files):
    import yaml

    validator_schema = json.loads((CACHE / "copilot-studio/schema/bot.schema.yaml-authoring.json").read_text()) \
        if (CACHE / "copilot-studio/schema/bot.schema.yaml-authoring.json").exists() else None
    if validator_schema is None:
        pytest.skip("Copilot Studio schema not fetched")
    for path, text in files.items():
        data = yaml.safe_load(text)
        kind = data.get("kind") if isinstance(data, dict) else None
        if kind is None:
            continue  # settings and connection references have no published schema
        validator = jsonschema.Draft7Validator({"$ref": f"#/definitions/{kind}", "definitions": validator_schema["definitions"]})
        error = best_match(validator.iter_errors(data))
        assert error is None, f"{path}: {list(error.absolute_path)}: {error.message[:300]}"


def validate(target, document):
    if target == "adl":
        validator = schema_validator("adl/schema", "0.3.0.json")
    elif target == "agent-spec":
        # The published schema puts agentspec_version beside a closed Agent definition,
        # so a versioned document can't validate as a whole. Check the parts separately.
        validator = schema_validator("agent-spec/schema", "agentspec_json_spec_26_3_1.json", "#/$defs/Agent")
        enum = json.loads((CACHE / "agent-spec/schema/agentspec_json_spec_26_3_1.json").read_text())
        assert document["agentspec_version"] in enum["$defs"]["AgentSpecVersionEnum"]["enum"]
        document = {k: v for k, v in document.items() if k != "agentspec_version"}
    else:
        return
    error = best_match(validator.iter_errors(document))
    assert error is None, f"{list(error.absolute_path)}: {error.message[:300]}"


@pytest.mark.parametrize("fmt, fixture", [s for s in SOURCES if s[0] in FULL])
def test_native_round_trip_is_lossless(fmt, fixture):
    for record in records(fmt, fixture):
        conversion = convert(record, fmt)
        assert conversion.report.lossy == [], f"{record.agent}:\n{conversion.report.text()}"
        assert conversion.read_back.version == record.version


@pytest.mark.parametrize("target", sorted(WRITERS))
@pytest.mark.parametrize("fmt, fixture", SOURCES)
def test_every_loss_is_explained(fmt, fixture, target):
    for record in records(fmt, fixture):
        conversion = convert(record, target)
        unexplained = [f.path for f in conversion.report.lossy if not f.note]
        assert unexplained == [], f"{record.agent} → {target}: unexplained {unexplained}"
        assert conversion.read_back.agent == record.agent, "the agent keeps its identity"
        assert conversion.read_back.platform == record.platform
        if conversion.files and target == "copilot-studio":
            validate_copilot_studio(conversion.files)
        else:
            validate(target, conversion.document)
