"""The Terraform reader: a plan or state maps an agent the same way CloudFormation does."""

import copy
import json
import textwrap

from marshal_ledger.readers.cloudformation import CloudFormationReader, load_template
from marshal_ledger.readers.terraform import TerraformReader

FAKE = "sk-" + "a1B2c3D4e5F6g7H8i9J0" * 2
INSTRUCTION = "Look up an order before answering. Refunds above 200 USD go to a person."
POLICY = {"Version": "2012-10-17", "Statement": [
    {"Effect": "Allow", "Action": "bedrock:InvokeModel", "Resource": "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-lite-v1:0"},
]}
LAMBDA_POLICY = {"Version": "2012-10-17", "Statement": [
    {"Effect": "Allow", "Action": "dynamodb:UpdateItem", "Resource": "arn:aws:dynamodb:us-east-1:123456789012:table/refunds"},
]}


def resource(address, kind, values, unknown=None, sensitive=None):
    name = address.split(".")[1]
    return {"address": address, "mode": "managed", "type": kind, "name": name, "values": values,
            "sensitive_values": sensitive or {}}, {"address": address, "type": kind,
                                                   "change": {"actions": ["create"], "after": values,
                                                              "after_unknown": unknown or {}, "after_sensitive": sensitive or {}}}


def plan(resources, expressions):
    """A plan as `terraform show -json` prints it, for resources in the root module."""
    planned, changes = zip(*resources)
    config = [{"address": a, "type": a.split(".")[0], "name": a.split(".")[1], "expressions": e} for a, e in expressions.items()]
    return {"format_version": "1.2", "terraform_version": "1.13.4", "planned_values": {"root_module": {"resources": list(planned)}},
            "resource_changes": list(changes), "configuration": {"root_module": {"resources": config}}}


def refunds_plan():
    return plan([
        resource("aws_iam_role.agent", "aws_iam_role", {"name": "refunds-agent", "inline_policy": []}, {"arn": True}),
        resource("aws_iam_role_policy.agent", "aws_iam_role_policy", {"name": "invoke", "role": "refunds-agent",
                                                                       "policy": json.dumps(POLICY)}),
        resource("aws_iam_role.fn", "aws_iam_role", {"name": "refunds-fn", "inline_policy": []}, {"arn": True}),
        resource("aws_iam_role_policy.fn", "aws_iam_role_policy", {"name": "write", "role": "refunds-fn",
                                                                    "policy": json.dumps(LAMBDA_POLICY)}),
        resource("aws_lambda_function.refunds", "aws_lambda_function", {"function_name": "refunds-handler"},
                 {"arn": True, "role": True}),
        resource("aws_bedrockagent_agent.refunds", "aws_bedrockagent_agent",
                 {"agent_name": "refunds", "foundation_model": "amazon.nova-lite-v1:0", "instruction": INSTRUCTION,
                  "description": "Issues refunds.", "idle_session_ttl_in_seconds": 600, "prepare_agent": True},
                 {"agent_resource_role_arn": True, "agent_id": True}),
        resource("aws_bedrockagent_agent_action_group.refunds", "aws_bedrockagent_agent_action_group",
                 {"action_group_name": "refunds", "action_group_state": "ENABLED", "agent_version": "DRAFT",
                  "action_group_executor": [{"custom_control": None}],
                  "function_schema": [{"member_functions": [{"functions": [
                      {"name": "issue_refund", "description": "Refund an order.",
                       "parameters": [{"map_block_key": "order_id", "type": "string", "required": True, "description": None}]},
                  ]}]}]},
                 {"agent_id": True, "action_group_executor": [{"lambda": True}]}),
    ], {
        "aws_lambda_function.refunds": {"role": {"references": ["aws_iam_role.fn.arn", "aws_iam_role.fn"]}},
        "aws_bedrockagent_agent.refunds": {"agent_resource_role_arn": {"references": ["aws_iam_role.agent.arn", "aws_iam_role.agent"]}},
        "aws_bedrockagent_agent_action_group.refunds": {
            "agent_id": {"references": ["aws_bedrockagent_agent.refunds.agent_id", "aws_bedrockagent_agent.refunds"]},
            "action_group_executor": [{"lambda": {"references": ["aws_lambda_function.refunds.arn", "aws_lambda_function.refunds"]}}],
        },
    })


CLOUDFORMATION = textwrap.dedent(f"""
    Resources:
      AgentRole:
        Type: AWS::IAM::Role
        Properties: {{RoleName: refunds-agent}}
      AgentPolicy:
        Type: AWS::IAM::Policy
        Properties:
          Roles: [!Ref AgentRole]
          PolicyDocument: {json.dumps(POLICY)}
      FnRole:
        Type: AWS::IAM::Role
        Properties: {{RoleName: refunds-fn}}
      FnPolicy:
        Type: AWS::IAM::Policy
        Properties:
          Roles: [!Ref FnRole]
          PolicyDocument: {json.dumps(LAMBDA_POLICY)}
      Fn:
        Type: AWS::Lambda::Function
        Properties: {{FunctionName: refunds-handler, Role: !GetAtt FnRole.Arn}}
      Agent:
        Type: AWS::Bedrock::Agent
        Metadata:
          io.github.marshal-ledger: {{agent: aws_bedrockagent_agent.refunds}}
        Properties:
          AgentName: refunds
          FoundationModel: amazon.nova-lite-v1:0
          Instruction: "{INSTRUCTION}"
          Description: Issues refunds.
          IdleSessionTTLInSeconds: 600
          AgentResourceRoleArn: !GetAtt AgentRole.Arn
          ActionGroups:
            - ActionGroupName: refunds
              ActionGroupState: ENABLED
              ActionGroupExecutor: {{Lambda: !GetAtt Fn.Arn}}
              FunctionSchema:
                Functions:
                  - Name: issue_refund
                    Description: Refund an order.
                    Parameters: {{order_id: {{Type: string, Required: true}}}}
""")


def without_basis(node):
    if isinstance(node, dict):
        return {k: without_basis(v) for k, v in node.items() if k != "basis"}
    if isinstance(node, list):
        return [without_basis(v) for v in node]
    return node


def read(data, artifact="refunds.plan.json"):
    return TerraformReader().read_data(data, artifact)


def test_an_agent_maps_the_same_from_terraform_and_cloudformation():
    [terraform] = read(refunds_plan()).records
    [cloudformation] = CloudFormationReader().read_data(load_template(CLOUDFORMATION), "refunds.yaml").records
    assert terraform.agent == cloudformation.agent == "aws_bedrockagent_agent.refunds"
    assert terraform.version == cloudformation.version
    # Only provenance differs: each names its own source's locations.
    assert without_basis(terraform.spec.model_dump(mode="json")) == without_basis(cloudformation.spec.model_dump(mode="json"))
    assert terraform.bindings == cloudformation.bindings == {
        "executors": {"tool/bedrock.aws_bedrockagent_agent.refunds.refunds.issue_refund": "function:refunds-handler"}}


def test_terraform_provenance_names_addresses_and_attributes():
    [record] = read(refunds_plan()).records
    assert (record.source.format, record.source.locator) == ("terraform", "aws_bedrockagent_agent.refunds")
    assert record.source.detected_by == "resource type aws_bedrockagent_agent"
    assert record.spec.model.requested.basis == "aws_bedrockagent_agent.refunds.foundation_model"
    assert record.spec.permissions.principal.basis == "aws_iam_role.agent.name"
    [tool] = record.spec.tools
    assert tool.scope.value.value == "write"  # the executor Lambda's role can update a DynamoDB table
    assert "aws_iam_role.fn" in tool.scope.basis


def test_agents_in_subfolders_keep_the_folder_in_their_id():
    [record] = read(refunds_plan(), "envs/prod/plan.json").records
    assert record.agent == "envs/prod/aws_bedrockagent_agent.refunds"


def test_sensitive_values_are_never_copied():
    data = refunds_plan()
    agent = next(r for r in data["planned_values"]["root_module"]["resources"] if r["type"] == "aws_bedrockagent_agent")
    agent["values"]["instruction"] = f"Use API key {FAKE} for refunds."
    agent["sensitive_values"] = {"instruction": True}
    [record] = read(data).records
    assert FAKE not in record.model_dump_json()
    assert not record.spec.instructions.hash


def test_unknown_values_from_no_resource_are_absent():
    data = refunds_plan()
    change = next(c for c in data["resource_changes"] if c["type"] == "aws_bedrockagent_agent")
    change["change"]["after_unknown"]["guardrail_configuration"] = True
    data["configuration"]["root_module"]["resources"][1]["expressions"]["guardrail_configuration"] = {
        "references": ["local.guardrail"]}  # a local: a plan doesn't record what it holds
    [record] = read(data).records
    assert "guardrail" not in record.spec.extensions["bedrock"]


def test_state_json_links_known_arns_to_their_resources():
    """`terraform show -json` of state: every value known, no configuration, references only by value."""
    data = refunds_plan()
    arns = {"aws_iam_role.agent": "arn:aws:iam::123456789012:role/refunds-agent",
            "aws_iam_role.fn": "arn:aws:iam::123456789012:role/refunds-fn",
            "aws_lambda_function.refunds": "arn:aws:lambda:us-east-1:123456789012:function:refunds-handler",
            "aws_bedrockagent_agent.refunds": "AGENT12345"}
    resources = copy.deepcopy(data["planned_values"]["root_module"]["resources"])
    for r in resources:
        values = r["values"]
        if r["address"] in arns:
            values["agent_id" if r["type"] == "aws_bedrockagent_agent" else "arn"] = arns[r["address"]]
        if r["type"] == "aws_lambda_function":
            values["role"] = arns["aws_iam_role.fn"]
        if r["type"] == "aws_bedrockagent_agent":
            values["agent_resource_role_arn"] = arns["aws_iam_role.agent"]
        if r["type"] == "aws_bedrockagent_agent_action_group":
            values["agent_id"] = arns["aws_bedrockagent_agent.refunds"]
            values["action_group_executor"] = [{"lambda": arns["aws_lambda_function.refunds"], "custom_control": None}]
    state = {"format_version": "1.0", "terraform_version": "1.13.4", "values": {"root_module": {"resources": resources}}}
    [record] = read(state, "state.json").records
    assert record.spec.permissions.principal.value == "refunds-agent"
    assert [t.name for t in record.spec.tools] == ["issue_refund"]
    assert record.spec.tools[0].scope.value.value == "write"
    assert record.bindings["executors"] and set(record.bindings["executors"].values()) == {"function:refunds-handler"}


def test_only_plans_and_states_with_agents_match():
    assert TerraformReader.matches(refunds_plan())
    assert not TerraformReader.matches({"terraform_version": "1.13.4", "planned_values": {"root_module": {}}})
    assert not TerraformReader.matches({"Resources": {}})


def test_a_link_lost_through_a_local_is_reported():
    data = refunds_plan()
    policy = next(c for c in data["resource_changes"] if c["address"] == "aws_iam_role_policy.agent")
    policy["change"]["after_unknown"]["role"] = True
    next(r for r in data["planned_values"]["root_module"]["resources"] if r["address"] == "aws_iam_role_policy.agent")["values"]["role"] = None
    data["configuration"]["root_module"]["resources"].append(
        {"address": "aws_iam_role_policy.agent", "type": "aws_iam_role_policy", "name": "agent",
         "expressions": {"role": {"references": ["local.role_name"]}}})
    result = read(data)
    [record] = result.records
    assert record.spec.permissions.grants == []
    assert result.warnings == ["refunds.plan.json: aws_iam_role_policy.agent: role comes from local.role_name, which a "
                               "Terraform plan doesn't record, so this policy's permissions aren't linked to a role"]


def test_a_plan_and_the_state_after_apply_give_the_same_version():
    """A plan leaves out values the agent doesn't set; the state after apply has Bedrock's defaults
    filled in. Both must record the same version, or every apply would look like a change."""
    data = refunds_plan()
    agent = next(r for r in data["planned_values"]["root_module"]["resources"] if r["type"] == "aws_bedrockagent_agent")
    del agent["values"]["idle_session_ttl_in_seconds"]
    [planned] = read(data).records
    applied = copy.deepcopy(data)
    values = next(r for r in applied["planned_values"]["root_module"]["resources"] if r["type"] == "aws_bedrockagent_agent")["values"]
    values.update({"idle_session_ttl_in_seconds": 600, "agent_collaboration": "DISABLED"})
    [after] = read(applied).records
    assert after.version == planned.version
    assert "idle_session_ttl" not in after.spec.extensions["bedrock"]
