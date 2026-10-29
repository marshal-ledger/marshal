"""The AgentCore CLI reader: a project's harness maps the same way as its CloudFormation resource."""

import json
import textwrap

import pytest

from marshal_ledger.convert import convert
from marshal_ledger.model import IdentityKind, Provenance
from marshal_ledger.readers import detect
from marshal_ledger.readers.agentcorecli import AgentCoreCliReader
from marshal_ledger.readers.cloudformation import CloudFormationReader, load_template

TOKEN = "sk-" + "q9W8e7R6t5Y4u3I2o1P0" * 2
PROMPT = "Answer questions about orders. Never promise a refund."

# harness.yaml as `agentcore add harness` writes it (schema version 2), with the
# sections the CLI's harness configuration guide documents.
HARNESS_YAML = textwrap.dedent(f"""
    name: support
    model:
      openAiModelConfig:
        modelId: gpt-6.1-sol
        apiFormat: responses
        apiKeyArn: arn:aws:bedrock-agentcore:us-west-2:123456789012:token-vault/default/apikeycredentialprovider/openai
        temperature: 0.2
    tools:
      - name: browser
        type: agentcore_browser
      - name: research
        type: remote_mcp
        config:
          remoteMcp:
            url: https://mcp.example.com/mcp
            headers:
              Authorization: Bearer {TOKEN}
      - name: company_tools
        type: agentcore_gateway
        config:
          agentCoreGateway:
            gatewayArn: arn:aws:bedrock-agentcore:us-west-2:123456789012:gateway/company-tools-abc123
            outboundAuth:
              awsIam: {{}}
      - name: approve_purchase
        type: inline_function
        config:
          inlineFunction:
            description: Request approval for a purchase.
            inputSchema:
              type: object
              properties:
                amount:
                  type: number
              required: [amount]
    allowedTools:
      - "@builtin"
      - "@research/search"
    memory:
      agentCoreMemoryConfiguration:
        name: ConversationMemory
        messagesCount: 20
    skills:
      - git:
          url: https://github.com/example/private-skills.git
          path: skills/support
          auth:
            credentialName: github-token
    maxIterations: 15
    timeoutSeconds: 300
    executionRoleArn: arn:aws:iam::123456789012:role/MyHarnessRole
    environmentVariables:
      LOG_LEVEL: info
      ORDERS_API_TOKEN: {TOKEN}
    environment:
      agentCoreRuntimeEnvironment:
        filesystemConfigurations:
          - sessionStorage:
              mountPath: /mnt/session
    dockerfile: Dockerfile
    tags:
      team: support
""")

# The same harness as a CloudFormation resource, with an agent id to match the project's.
CLOUDFORMATION = textwrap.dedent(f"""
    Resources:
      support:
        Type: AWS::BedrockAgentCore::Harness
        Metadata:
          io.github.marshal-ledger: {{agent: shop/support}}
        Properties:
          HarnessName: support
          ExecutionRoleArn: arn:aws:iam::123456789012:role/MyHarnessRole
          Model:
            OpenAiModelConfig:
              ModelId: gpt-6.1-sol
              ApiFormat: responses
              ApiKeyArn: arn:aws:bedrock-agentcore:us-west-2:123456789012:token-vault/default/apikeycredentialprovider/openai
              Temperature: 0.2
          SystemPrompt:
            - Text: {PROMPT}
          Tools:
            - {{Name: browser, Type: agentcore_browser}}
            - Name: research
              Type: remote_mcp
              Config: {{RemoteMcp: {{Url: "https://mcp.example.com/mcp", Headers: {{Authorization: "Bearer {TOKEN}"}}}}}}
            - Name: company_tools
              Type: agentcore_gateway
              Config:
                AgentCoreGateway:
                  GatewayArn: arn:aws:bedrock-agentcore:us-west-2:123456789012:gateway/company-tools-abc123
                  OutboundAuth: {{AwsIam: {{}}}}
            - Name: approve_purchase
              Type: inline_function
              Config:
                InlineFunction:
                  Description: Request approval for a purchase.
                  InputSchema: {{type: object, properties: {{amount: {{type: number}}}}, required: [amount]}}
          AllowedTools: ["@builtin", "@research/search"]
          Memory: {{AgentCoreMemoryConfiguration: {{Name: ConversationMemory, MessagesCount: 20}}}}
          Skills:
            - Git: {{Url: "https://github.com/example/private-skills.git", Path: skills/support, Auth: {{CredentialName: github-token}}}}
          MaxIterations: 15
          TimeoutSeconds: 300
          EnvironmentVariables: {{LOG_LEVEL: info, ORDERS_API_TOKEN: "{TOKEN}"}}
          Environment:
            AgentCoreRuntimeEnvironment:
              FilesystemConfigurations:
                - SessionStorage: {{MountPath: /mnt/session}}
""")

# The same harness in a version 1 project: harness.json, in the older flat shape.
HARNESS_JSON = {
    "name": "support",
    "model": {"provider": "open_ai", "modelId": "gpt-6.1-sol", "apiFormat": "responses", "temperature": 0.2,
              "apiKeyArn": "arn:aws:bedrock-agentcore:us-west-2:123456789012:token-vault/default/apikeycredentialprovider/openai"},
    "tools": [
        {"name": "browser", "type": "agentcore_browser"},
        {"name": "research", "type": "remote_mcp",
         "config": {"remoteMcp": {"url": "https://mcp.example.com/mcp", "headers": {"Authorization": f"Bearer {TOKEN}"}}}},
        {"name": "company_tools", "type": "agentcore_gateway", "config": {"agentCoreGateway": {
            "gatewayArn": "arn:aws:bedrock-agentcore:us-west-2:123456789012:gateway/company-tools-abc123",
            "outboundAuth": {"awsIam": {}}}}},
        {"name": "approve_purchase", "type": "inline_function", "config": {"inlineFunction": {
            "description": "Request approval for a purchase.",
            "inputSchema": {"type": "object", "properties": {"amount": {"type": "number"}}, "required": ["amount"]}}}},
    ],
    "allowedTools": ["@builtin", "@research/search"],
    "memory": {"name": "ConversationMemory", "messagesCount": 20},
    "skills": [{"gitUrl": "https://github.com/example/private-skills.git", "path": "skills/support",
                "auth": {"credentialName": "github-token"}}],
    "maxIterations": 15,
    "timeoutSeconds": 300,
    "executionRoleArn": "arn:aws:iam::123456789012:role/MyHarnessRole",
    "environmentVariables": {"LOG_LEVEL": "info", "ORDERS_API_TOKEN": TOKEN},
    "sessionStoragePath": "/mnt/session",
    "networkMode": "PUBLIC",
    "dockerfile": "Dockerfile",
}


def project(root, harness=HARNESS_YAML, *, version=2, file="harness.yaml", prompt=PROMPT, name="shop", path="app/support"):
    (root / "agentcore").mkdir(parents=True)
    registry = {"name": name, "version": version, "managedBy": "CDK", "runtimes": [], "memories": [{"name": "ConversationMemory"}],
                "harnesses": [{"name": "support", "path": path}]}
    (root / "agentcore" / "agentcore.json").write_text(json.dumps(registry))
    folder = root / "app" / "support"
    folder.mkdir(parents=True)
    (folder / file).write_text(harness if isinstance(harness, str) else json.dumps(harness))
    if prompt is not None:
        (folder / "system-prompt.md").write_text(prompt)
    return root


def read(path):
    result = AgentCoreCliReader().read(path)
    [record] = result.records
    return record


def without_basis(node):
    if isinstance(node, dict):
        return {k: without_basis(v) for k, v in node.items() if k != "basis"}
    if isinstance(node, list):
        return [without_basis(v) for v in node]
    return node


def test_a_harness_maps_the_same_from_the_cli_project_and_cloudformation(tmp_path):
    cli = read(project(tmp_path))
    [cfn] = CloudFormationReader().read_data(load_template(CLOUDFORMATION), "stack.yaml").records
    assert (cli.agent, cli.platform) == ("shop/support", "bedrock-agentcore")
    cli_spec = cli.spec.model_dump(mode="json")
    cfn_spec = cfn.spec.model_dump(mode="json")
    # The project states two settings the resource has no property for.
    assert cli_spec["extensions"]["bedrock-agentcore"].pop("dockerfile") == "Dockerfile"
    assert without_basis(cli_spec) == without_basis(cfn_spec)
    assert cli.bindings == cfn.bindings


def test_harness_mapping(tmp_path):
    record = read(project(tmp_path))
    spec = record.spec
    assert spec.model.requested.value == "gpt-6.1-sol"
    assert spec.model.provider.value == "openai"
    assert spec.model.parameters == {"temperature": 0.2}
    assert record.bindings["model_api_key"].endswith("apikeycredentialprovider/openai")
    assert spec.instructions.text == PROMPT
    assert spec.permissions.principal.value == "arn:aws:iam::123456789012:role/MyHarnessRole"
    tools = {t.ref: t for t in spec.tools}
    assert set(tools) == {
        "tool/bedrock-agentcore.browser", "tool/mcp.https://mcp.example.com/mcp",
        "tool/bedrock-agentcore.gateway.arn:aws:bedrock-agentcore:us-west-2:123456789012:gateway/company-tools-abc123",
        "tool/bedrock-agentcore.shop.support.approve_purchase",
        # AllowedTools has @builtin, so the default tools stay available.
        "tool/bedrock-agentcore.shell", "tool/bedrock-agentcore.file_operations",
    }
    assert tools["tool/bedrock-agentcore.shop.support.approve_purchase"].input_schema["required"] == ["amount"]
    ext = spec.extensions["bedrock-agentcore"]
    assert ext["memory"] == {"AgentCoreMemoryConfiguration": {"Name": "ConversationMemory", "MessagesCount": 20}}
    assert ext["skills"][0]["Git"]["Auth"] == {"CredentialName": "github-token"}


def test_provenance_names_the_project_files(tmp_path):
    record = read(project(tmp_path))
    assert (record.source.format, record.source.artifact, record.source.locator) == (
        "agentcore-cli", "app/support/harness.yaml", "harness support")
    assert record.source.detected_by == "harness registered in agentcore/agentcore.json"
    assert record.spec.model.requested.basis == "model.openAiModelConfig.modelId"
    assert record.spec.instructions.basis == "app/support/system-prompt.md"
    assert record.spec.display_name.basis == "name"


def test_secret_values_are_not_recorded(tmp_path):
    harness = HARNESS_YAML + textwrap.dedent("""
        connections:
          - to:
              type: memory
              arn: arn:aws:bedrock-agentcore:us-west-2:123456789012:memory/SharedMemory-abc123
            access: read
    """)
    record = read(project(tmp_path, harness, prompt=f"Use the key {TOKEN} to call the orders API."))
    dumped = record.model_dump_json()
    assert TOKEN not in dumped
    ext = record.spec.extensions["bedrock-agentcore"]
    assert ext["mcp_headers"] == {"research": ["Authorization"]}
    assert ext["environment_variables"] == ["LOG_LEVEL", "ORDERS_API_TOKEN"]
    assert ext["connections"][0]["access"] == "read"
    assert "[REDACTED]" in record.spec.instructions.text


def test_model_parameters_that_can_hold_credentials_keep_their_names_only(tmp_path):
    harness = textwrap.dedent(f"""
        name: support
        model:
          liteLlmModelConfig:
            modelId: litellm_proxy/my-model
            apiBase: https://models.example.com/v1
            additionalParams:
              api_key: {TOKEN}
              extra_headers: {{X-Team-Token: {TOKEN}}}
              drop_params: true
              metadata: {{note: "fallback key {TOKEN}"}}
    """)
    record = read(project(tmp_path, harness))
    assert TOKEN not in record.model_dump_json()
    params = record.spec.extensions["bedrock-agentcore"]["model_config"]["AdditionalParams"]
    assert params["api_key"] == params["extra_headers"] == "[REDACTED]"
    assert params["drop_params"] is True
    assert params["metadata"] == {"note": "fallback key [REDACTED]"}


def test_harness_json_and_harness_yaml_give_the_same_version(tmp_path):
    yaml_record = read(project(tmp_path / "v2"))
    json_record = read(project(tmp_path / "v1", HARNESS_JSON, version=1, file="harness.json"))
    assert json_record.version == yaml_record.version
    assert json_record.source.artifact == "app/support/harness.json"
    assert json_record.spec.model.requested.basis == "model.modelId"


def test_an_inline_system_prompt_wins_over_the_file(tmp_path):
    harness = HARNESS_YAML + "systemPrompt:\n  - text: Inline instructions.\n"
    record = read(project(tmp_path, harness))
    assert record.spec.instructions.text == "Inline instructions."
    assert record.spec.instructions.basis == "systemPrompt"


def test_a_harness_without_instructions_says_so(tmp_path):
    record = read(project(tmp_path, prompt=None))
    assert record.spec.instructions.hash is None
    assert record.spec.instructions.basis == "systemPrompt is not set"


def test_a_harness_without_a_role_gets_one_from_deploy(tmp_path):
    harness = HARNESS_YAML.replace("executionRoleArn: arn:aws:iam::123456789012:role/MyHarnessRole\n", "")
    permissions = read(project(tmp_path, harness)).spec.permissions
    assert permissions.identity.value is IdentityKind.SERVICE_PRINCIPAL
    assert permissions.identity.provenance is Provenance.DERIVED
    assert "agentcore deploy creates an execution role" in permissions.identity.basis
    assert not permissions.principal.is_known


@pytest.mark.parametrize("path", ["../elsewhere", "/etc/harness", "C:/harness"])
def test_a_harness_outside_the_project_is_skipped(tmp_path, path):
    result = AgentCoreCliReader().read(project(tmp_path, path=path))
    assert not result.records
    [skipped] = result.skipped
    assert "outside the project" in skipped.reason


def test_a_missing_configuration_is_skipped(tmp_path):
    root = project(tmp_path)
    (root / "app" / "support" / "harness.yaml").unlink()
    result = AgentCoreCliReader().read(root)
    assert not result.records
    assert result.skipped[0].reason == "harness configuration not found or not readable"


def test_projects_are_found_below_the_path_and_named_relative_to_it(tmp_path):
    project(tmp_path / "services" / "shop")
    record = read(tmp_path)
    assert record.source.artifact == "services/shop/app/support/harness.yaml"
    assert record.spec.instructions.basis == "services/shop/app/support/system-prompt.md"
    # Pointing at the registry itself reads the same harness.
    assert read(tmp_path / "services" / "shop" / "agentcore" / "agentcore.json").version == record.version


def test_detection(tmp_path):
    root = project(tmp_path / "harness")
    assert detect(root) is AgentCoreCliReader
    assert detect(root / "agentcore") is AgentCoreCliReader
    # A project with runtimes only is agent code: the Python reader reads it.
    code = tmp_path / "runtime"
    (code / "agentcore").mkdir(parents=True)
    (code / "agentcore" / "agentcore.json").write_text(json.dumps({"name": "code", "version": 2, "runtimes": [{"name": "a"}]}))
    (code / "app").mkdir()
    (code / "app" / "main.py").write_text("from strands import Agent\nagent = Agent(system_prompt='Hi')\n")
    assert not AgentCoreCliReader.detect(code)
    assert detect(code) is not AgentCoreCliReader


def test_a_project_memory_becomes_an_arn_parameter_in_cloudformation(tmp_path):
    harness = HARNESS_YAML.replace("    messagesCount: 20\n", "    messagesCount: 20\n    retrievalConfig:\n      topK: 5\n")
    assert "retrievalConfig" in harness
    record = read(project(tmp_path, harness))
    conversion = convert(record, "agentcore-harness")
    [resource] = conversion.document["Resources"].values()
    memory = resource["Properties"]["Memory"]["AgentCoreMemoryConfiguration"]
    assert memory == {"Arn": {"Ref": "ConversationMemoryMemoryArn"}, "MessagesCount": 20}
    assert "ConversationMemoryMemoryArn" in conversion.document["Parameters"]
    lossy = {f.path: f.note for f in conversion.report.lossy}
    assert "takes its ARN as a parameter" in lossy["extensions.bedrock-agentcore.memory"]
    assert "every namespace" in lossy["extensions.bedrock-agentcore.memory"]
