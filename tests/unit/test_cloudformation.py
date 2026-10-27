import re
import textwrap

from marshal_ledger.convert import convert
from marshal_ledger.model import Scope
from marshal_ledger.readers.cloudformation import CloudFormationReader, action_scope, load_template

TEMPLATE = textwrap.dedent("""
    Parameters:
      ModelId: {Type: String, Default: anthropic.claude-sonnet-4-5-v1:0}
    Resources:
      Role:
        Type: AWS::IAM::Role
        Properties:
          RoleName: invoice-agent-role
          Policies:
            - PolicyDocument:
                Statement:
                  - {Effect: Allow, Action: [bedrock:InvokeModel], Resource: "*"}
                  - {Effect: Deny, Action: [s3:DeleteObject], Resource: "*"}
      Agent:
        Type: AWS::Bedrock::Agent
        Properties:
          AgentName: invoice-agent
          FoundationModel: !Ref ModelId
          Instruction: Reconcile vendor invoices against purchase orders and flag mismatches.
          AgentResourceRoleArn: !GetAtt Role.Arn
          ActionGroups:
            - ActionGroupName: erp
              ActionGroupExecutor: {CustomControl: RETURN_CONTROL}
              FunctionSchema:
                Functions:
                  - Name: approve_payment
                    Description: Approve a payment.
                    RequireConfirmation: ENABLED
                    Parameters:
                      invoice_id: {Type: string, Required: true}
""")


def test_short_form_tags_load():
    data = load_template("A: !Sub '${B}-x'\nC: !GetAtt R.Arn\nD: !Ref P\n")
    assert data == {"A": {"Fn::Sub": "${B}-x"}, "C": {"Fn::GetAtt": ["R", "Arn"]}, "D": {"Ref": "P"}}


def test_action_scope_by_verb():
    assert action_scope("s3:GetObject") is Scope.READ
    assert action_scope("bedrock:InvokeModel") is Scope.EXECUTE
    assert action_scope("dynamodb:PutItem") is Scope.WRITE
    assert action_scope("s3:*") is Scope.ADMIN


def test_agent_mapping_and_round_trip():
    [record] = CloudFormationReader().read_data(load_template(TEMPLATE), "stack.yaml").records
    spec = record.spec
    assert record.agent == "stack/Agent"
    assert spec.model.requested.value == "anthropic.claude-sonnet-4-5-v1:0"
    assert spec.permissions.principal.value == "invoice-agent-role"
    assert [(g.privilege, g.scope) for g in spec.permissions.grants] == [("bedrock:InvokeModel", Scope.EXECUTE)]
    [tool] = spec.tools
    assert tool.requires_confirmation.value is True
    assert tool.input_schema == {"type": "object", "properties": {"invoice_id": {"type": "string"}}, "required": ["invoice_id"]}

    conversion = convert(record, "cloudformation")
    assert conversion.read_back.agent == record.agent
    assert {f.path for f in conversion.report.lossy} <= {
        "permissions.principal", "permissions.grants[other/aws.* bedrock:InvokeModel]"
    }
    assert all(f.note for f in conversion.report.lossy)


KB_TEMPLATE = textwrap.dedent("""
    Resources:
      Agent:
        Type: AWS::Bedrock::Agent
        Properties:
          AgentName: kb-agent
          Instruction: Answer questions from the knowledge base and cite every source you use.
          AgentResourceRoleArn: arn:aws:iam::123456789012:role/kb-agent
          KnowledgeBases:
            - {KnowledgeBaseId: !ImportValue kb-stack-KnowledgeBaseId, Description: Product docs}
          GuardrailConfiguration:
            GuardrailIdentifier: !GetAtt Guardrail.GuardrailId
            GuardrailVersion: !GetAtt Guardrail.Version
""")


def agent_properties(conversion):
    [resource] = conversion.document["Resources"].values()
    return resource["Properties"]


def test_unresolved_references_are_written_as_cloudformation():
    [record] = CloudFormationReader().read_data(load_template(KB_TEMPLATE), "stack.yaml").records
    conversion = convert(record, "cloudformation")
    props = agent_properties(conversion)
    assert props["KnowledgeBases"][0]["KnowledgeBaseId"] == {"Fn::ImportValue": "kb-stack-KnowledgeBaseId"}
    assert props["GuardrailConfiguration"] == {
        "GuardrailIdentifier": {"Ref": "GuardrailGuardrailId"}, "GuardrailVersion": {"Ref": "GuardrailVersion"},
    }
    assert conversion.document["Parameters"]["GuardrailGuardrailId"]["Description"] == \
        "Stands in for Guardrail.GuardrailId in the source template"
    # The import reads back unchanged; the guardrail becomes parameters, which the report explains.
    lossy = {f.path: f.note for f in conversion.report.lossy}
    assert "extensions.bedrock.guardrail" in lossy
    assert not any(path.startswith("knowledge[") for path in lossy)
    assert all(lossy.values())


def test_function_parameter_names_are_made_valid():
    template = TEMPLATE.replace("invoice_id: {Type: string, Required: true}", "Input invoice_id: {Type: string, Required: true}")
    [record] = CloudFormationReader().read_data(load_template(template), "stack.yaml").records
    conversion = convert(record, "cloudformation")
    [function] = agent_properties(conversion)["ActionGroups"][0]["FunctionSchema"]["Functions"]
    assert list(function["Parameters"]) == ["Input-invoice_id"]
    assert any("parameter names" in (f.note or "") for f in conversion.report.lossy)


def test_written_agents_are_prepared_on_deploy():
    [record] = CloudFormationReader().read_data(load_template(TEMPLATE), "stack.yaml").records
    assert agent_properties(convert(record, "cloudformation"))["AutoPrepare"] is True


def test_multi_line_instructions_are_written_as_a_block():
    template = TEMPLATE.replace(
        "Instruction: Reconcile vendor invoices against purchase orders and flag mismatches.",
        "Instruction: |\n            Reconcile vendor invoices against purchase orders.\n            Flag every mismatch for review.",
    )
    [record] = CloudFormationReader().read_data(load_template(template), "stack.yaml").records
    assert "Instruction: |\n        Reconcile vendor invoices" in convert(record, "cloudformation").text


def test_functions_without_a_stated_confirmation_are_warned_about():
    template = "\n".join(line for line in TEMPLATE.splitlines() if "RequireConfirmation" not in line)
    [record] = CloudFormationReader().read_data(load_template(template), "stack.yaml").records
    conversion = convert(record, "cloudformation")
    assert "RequireConfirmation" not in conversion.text
    assert any("whether approve_payment needs confirmation" in w for w in conversion.warnings)


def test_values_equal_to_bedrock_defaults_are_recorded_as_absent():
    template = re.sub(r"( +)AgentName: invoice-agent\n",
                      r"\g<0>\1IdleSessionTTLInSeconds: 600\n\1AgentCollaboration: DISABLED\n", TEMPLATE)
    [stated] = CloudFormationReader().read_data(load_template(template), "stack.yaml").records
    [default] = CloudFormationReader().read_data(load_template(TEMPLATE), "stack.yaml").records
    assert "IdleSessionTTLInSeconds: 600" in template
    assert stated.version == default.version
    longer = template.replace("IdleSessionTTLInSeconds: 600", "IdleSessionTTLInSeconds: 1800")
    [changed] = CloudFormationReader().read_data(load_template(longer), "stack.yaml").records
    assert changed.spec.extensions["bedrock"]["idle_session_ttl"] == 1800
