import json
import textwrap

import pytest

from marshal_ledger.convert import convert
from marshal_ledger.model import Scope
from marshal_ledger.readers.cloudformation import CloudFormationReader, load_template

HARNESS = textwrap.dedent("""
    Resources:
      Role:
        Type: AWS::IAM::Role
        Properties:
          RoleName: support-harness-role
          Policies:
            - PolicyDocument:
                Statement:
                  - {Effect: Allow, Action: [bedrock:InvokeModel], Resource: "*"}
      LookupFunction:
        Type: AWS::Lambda::Function
        Properties:
          FunctionName: order-lookup
          Role: arn:aws:iam::123456789012:role/order-lookup
      Gateway:
        Type: AWS::BedrockAgentCore::Gateway
        Properties: {Name: orders, ProtocolType: MCP, AuthorizerType: AWS_IAM, RoleArn: arn:aws:iam::123456789012:role/gw}
      Target:
        Type: AWS::BedrockAgentCore::GatewayTarget
        Properties:
          GatewayIdentifier: !Ref Gateway
          Name: orders
          TargetConfiguration:
            Mcp:
              Lambda:
                LambdaArn: !GetAtt LookupFunction.Arn
                ToolSchema:
                  InlinePayload:
                    - Name: lookup_order
                      Description: Look up an order.
                      InputSchema:
                        Type: object
                        Properties: {order_id: {Type: string}}
                        Required: [order_id]
      Harness:
        Type: AWS::BedrockAgentCore::Harness
        Properties:
          HarnessName: support
          ExecutionRoleArn: !GetAtt Role.Arn
          Model:
            BedrockModelConfig:
              ModelId: global.anthropic.claude-sonnet-4-6
              Temperature: 0.2
              AdditionalParams:
                guardrailConfig: {guardrailIdentifier: gr-123, guardrailVersion: "1"}
          SystemPrompt:
            - Text: Help customers with their orders.
          AllowedTools: ["@orders", "@docs/*", "refund"]
          EnvironmentVariables: {SUPPORT_TOKEN: do-not-copy-this-value}
          Tools:
            - Type: agentcore_gateway
              Name: orders
              Config: {AgentCoreGateway: {GatewayArn: !GetAtt Gateway.GatewayArn}}
            - Type: remote_mcp
              Name: docs
              Config:
                RemoteMcp:
                  Url: https://mcp.example.com/docs
                  Headers: {Authorization: Bearer sk-live-do-not-copy}
            - Type: inline_function
              Name: refund
              Config:
                InlineFunction:
                  Description: Refund an order.
                  InputSchema: {type: object, properties: {order_id: {type: string}}}
""")


def read(text=HARNESS):
    [record] = CloudFormationReader().read_data(load_template(text), "stack.yaml").records
    return record


def test_harness_mapping():
    record = read()
    spec = record.spec
    assert (record.agent, record.platform) == ("stack/Harness", "bedrock-agentcore")
    assert spec.model.requested.value == "global.anthropic.claude-sonnet-4-6"
    assert spec.model.provider.value == "anthropic"
    assert spec.model.parameters == {"temperature": 0.2}
    assert spec.instructions.text == "Help customers with their orders."
    assert spec.permissions.principal.value == "support-harness-role"
    ext = spec.extensions["bedrock-agentcore"]
    assert ext["guardrail"] == {"guardrailIdentifier": "gr-123", "guardrailVersion": "1"}
    tools = {t.ref: t for t in spec.tools}
    # The gateway's tools, one by one, with the Lambda that runs them.
    lookup = tools["tool/bedrock-agentcore.gateway.stack.Gateway.orders.lookup_order"]
    assert lookup.input_schema == {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]}
    assert record.bindings["executors"][lookup.ref] == "function:order-lookup"
    assert "tool/mcp.https://mcp.example.com/docs" in tools
    assert tools["tool/bedrock-agentcore.stack.Harness.refund"].description == "Refund an order."
    # AllowedTools names no built-in tool, so the default shell and file tools are not available.
    assert not any(ref.endswith((".shell", ".file_operations")) for ref in tools)


def test_secret_values_are_not_recorded():
    record = read()
    dumped = json.dumps(record.model_dump(mode="json"))
    assert "sk-live-do-not-copy" not in dumped
    assert "do-not-copy-this-value" not in dumped
    ext = record.spec.extensions["bedrock-agentcore"]
    assert ext["mcp_headers"] == {"docs": ["Authorization"]}
    assert ext["environment_variables"] == ["SUPPORT_TOKEN"]


@pytest.mark.parametrize("allowed, has_shell", [
    (None, True), (["*"], True), (["@builtin"], True), (["@builtin/shell"], True), (["sh*"], True),
    ([], False), (["@docs"], False), (["@*/pc_*"], False), (["file_operations"], False),
])
def test_default_tools_follow_allowed_tools(allowed, has_shell):
    harness = {"Type": "AWS::BedrockAgentCore::Harness", "Properties": {
        "HarnessName": "h", "ExecutionRoleArn": "arn:aws:iam::123456789012:role/h",
        "Model": {"BedrockModelConfig": {"ModelId": "us.amazon.nova-2-lite-v1:0"}}}}
    if allowed is not None:
        harness["Properties"]["AllowedTools"] = allowed
    [record] = CloudFormationReader().read_data({"Resources": {"H": harness}}, "s.yaml").records
    tools = {t.ref: t for t in record.spec.tools}
    assert ("tool/bedrock-agentcore.shell" in tools) is has_shell
    if has_shell:
        assert tools["tool/bedrock-agentcore.shell"].scope.value is Scope.EXECUTE


def harness_props(conversion):
    [resource] = conversion.document["Resources"].values()
    assert resource["Type"] == "AWS::BedrockAgentCore::Harness"
    return resource["Properties"]


BEDROCK_AGENT = textwrap.dedent("""
    Resources:
      Agent:
        Type: AWS::Bedrock::Agent
        Properties:
          AgentName: refund-assistant
          FoundationModel: anthropic.claude-sonnet-4-5-20250929-v1:0
          AgentResourceRoleArn: arn:aws:iam::123456789012:role/refund-assistant
          Instruction: Help customers whose shipments arrived late. Never refund over $500 without approval.
          GuardrailConfiguration: {GuardrailIdentifier: gr-abc, GuardrailVersion: "2"}
          ActionGroups:
            - ActionGroupName: refunds
              ActionGroupExecutor: {CustomControl: RETURN_CONTROL}
              FunctionSchema:
                Functions:
                  - Name: issue_refund
                    Description: Refund an order.
                    RequireConfirmation: ENABLED
                    Parameters: {order_id: {Type: string, Required: true}}
""")


def test_bedrock_agent_moves_to_a_harness():
    record = read(BEDROCK_AGENT)
    conversion = convert(record, "agentcore-harness")
    props = harness_props(conversion)
    assert props["HarnessName"] == "refund_assistant"
    assert props["ExecutionRoleArn"] == "arn:aws:iam::123456789012:role/refund-assistant"
    model = props["Model"]["BedrockModelConfig"]
    assert model["ModelId"] == "anthropic.claude-sonnet-4-5-20250929-v1:0"
    assert model["AdditionalParams"]["guardrailConfig"] == {"guardrailIdentifier": "gr-abc", "guardrailVersion": "2"}
    [tool] = props["Tools"]
    assert tool["Type"] == "inline_function" and tool["Name"] == "issue_refund"
    # No shell or file tools: the Bedrock agent had none.
    assert props["AllowedTools"] == ["issue_refund"]
    # The agent keeps its id and tool refs, so its history continues on the new platform.
    assert conversion.read_back.agent == record.agent
    assert [t.ref for t in conversion.read_back.spec.tools] == [t.ref for t in record.spec.tools]
    lossy = {f.path: f.note for f in conversion.report.lossy}
    assert "confirm each call" in lossy[f"tools[{record.spec.tools[0].ref}].requires_confirmation"]
    assert all(lossy.values())


def test_written_harness_never_gains_the_default_tools():
    record = read(BEDROCK_AGENT)
    read_back = convert(record, "agentcore-harness").read_back
    assert not any(t.ref.startswith("tool/bedrock-agentcore.") and t.name in ("shell", "file_operations")
                   for t in read_back.spec.tools)


def test_harness_round_trip_keeps_the_definition():
    record = read()
    conversion = convert(record, "agentcore-harness")
    props = harness_props(conversion)
    assert props["AllowedTools"] == ["@orders", "@docs/*", "refund"]
    assert props["Model"]["BedrockModelConfig"]["AdditionalParams"]["guardrailConfig"]["guardrailIdentifier"] == "gr-123"
    # The gateway is in the source stack: the harness references it by a parameter.
    assert props["Tools"][0]["Config"]["AgentCoreGateway"]["GatewayArn"] == {"Ref": "OrdersGatewayArn"}
    assert "OrdersGatewayArn" in conversion.document["Parameters"]
    assert all(f.note for f in conversion.report.lossy)
    read_back = conversion.read_back.spec
    assert read_back.model == record.spec.model
    assert read_back.instructions == record.spec.instructions


def test_a_model_from_another_provider_gets_its_own_configuration():
    record = read(BEDROCK_AGENT)
    record.spec.model.requested.value = "gpt-5"
    record.spec.model.provider.value = "openai"
    props = harness_props(convert(record, "agentcore-harness"))
    config = props["Model"]["OpenAiModelConfig"]
    assert config["ModelId"] == "gpt-5"
    assert config["ApiKeyArn"] == {"Ref": "ModelApiKeyArn"}
