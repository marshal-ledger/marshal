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
