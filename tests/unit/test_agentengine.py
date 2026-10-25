"""Agent Engine responses, shaped as the reasoningEngines API returns them (aiplatform v1)."""

from marshal_ledger.model import IdentityKind, Provenance
from marshal_ledger.readers.agentengine import AgentEngineReader

FAKE = "sk-" + "a1B2c3D4e5F6g7H8i9J0" * 2

LIST = {
    "reasoningEngines": [
        {
            "name": "projects/123/locations/us-central1/reasoningEngines/456",
            "displayName": "Claims Assistant",
            "description": "Answers claims questions.",
            "spec": {
                "agentFramework": "google-adk",
                "identityType": "AGENT_IDENTITY",
                "classMethods": [{"name": "stream_query"}, {"name": "create_session"}],
                "deploymentSpec": {
                    "env": [{"name": "MODEL", "value": "gemini-2.5-flash"}, {"name": "API_KEY", "value": FAKE}],
                    "secretEnv": [{"name": "DB_PASSWORD", "secretRef": {"secret": "claims-db", "version": "3"}}],
                },
                "sourceCodeSpec": {
                    "developerConnectSource": {"config": {"gitRepositoryLink": "projects/123/locations/us-central1/connections/gh/gitRepositoryLinks/claims", "dir": "agent", "revision": "main"}},
                    "pythonSpec": {"entrypointModule": "agent", "entrypointObject": "root_agent"},
                },
            },
            "createTime": "2026-09-01T10:00:00Z",
            "labels": {"team": "claims"},
        },
        {
            "name": "projects/123/locations/us-central1/reasoningEngines/789",
            "displayName": "Legacy Bot",
            "spec": {"serviceAccount": "legacy-bot@proj.iam.gserviceaccount.com", "packageSpec": {"pickleObjectGcsUri": "gs://b/agent.pkl"}},
        },
    ]
}


def test_inventory_from_saved_list_response():
    records = {r.agent: r for r in AgentEngineReader().read_data(LIST, "engines.json").records}
    claims = records["vertex-agent-engine/claims-assistant"]
    assert claims.spec.permissions.identity.value is IdentityKind.SERVICE_PRINCIPAL
    assert claims.spec.permissions.identity.provenance is Provenance.DECLARED
    assert claims.spec.model.requested.provenance is Provenance.UNKNOWN
    ext = claims.spec.extensions["vertex-agent-engine"]
    assert ext["framework"] == "google-adk"
    assert ext["environment_variables"] == ["API_KEY", "MODEL"]
    assert ext["secret_environment_variables"] == [{"name": "DB_PASSWORD", "secret": "claims-db"}]
    assert ext["source"]["dir"] == "agent"
    assert FAKE not in claims.model_dump_json(), "environment values are never copied"
    assert claims.bindings["resource_name"].endswith("/456")
    legacy = records["vertex-agent-engine/legacy-bot"]
    assert legacy.spec.permissions.principal.value == "legacy-bot@proj.iam.gserviceaccount.com"


def test_single_get_response_matches():
    engine = LIST["reasoningEngines"][1]
    assert AgentEngineReader.matches(engine)
    assert not AgentEngineReader.matches({"name": "projects/1/locations/x/models/2"})
