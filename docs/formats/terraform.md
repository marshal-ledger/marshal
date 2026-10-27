---
title: "Bedrock agent inventory from Terraform plans and state"
description: >-
  How Marshal reads Bedrock Agents and AgentCore from Terraform plan and state JSON, for the aws and awscc providers, in CI before apply.
---

# Terraform: Bedrock Agents and AgentCore

Format id: `terraform`. Reads the JSON that `terraform show -json` prints for a saved plan, or for the current state. Marshal never runs Terraform: the pipeline that already plans and applies produces the file. OpenTofu's `tofu show -json` output works the same way.

| Resources | Platform id | Tier |
|---|---|---|
| `aws_bedrockagent_agent` with `aws_bedrockagent_agent_action_group`, `aws_bedrockagent_agent_knowledge_base_association`, `aws_bedrockagent_agent_collaborator` and `aws_bedrockagent_agent_alias` (AWS provider) | `bedrock` | Read in full, as for CloudFormation |
| `awscc_bedrock_agent` and `awscc_bedrock_agent_alias` (Cloud Control provider) | `bedrock` | Read in full, as for CloudFormation |
| `aws_bedrockagentcore_agent_runtime` and its endpoints, `awscc_bedrockagentcore_runtime` | `bedrock-agentcore` | Inventory, as for CloudFormation |

The agent id is the resource's address, such as `module.bedrock.awscc_bedrock_agent.bedrock_agent[0]`, prefixed with the plan file's folder when it isn't at the root of what was read, such as `envs/prod/aws_bedrockagent_agent.refunds`.

## Same mapping as CloudFormation

The AWS and Cloud Control providers describe the same Bedrock resources CloudFormation does, with the same fields in snake_case. Marshal translates the plan's agents, and the IAM roles, role policies, policy attachments, Lambda functions and knowledge bases they refer to, into their CloudFormation form, then reads them with the [CloudFormation](cloudformation.md) mapping. An agent therefore maps the same way whichever tool defines it, and the same definition gets the same version. Provenance names the Terraform address and attribute, such as `aws_bedrockagent_agent.forex_asst.foundation_model`.

A plan of the public [acwwat/terraform-amazon-bedrock-agent-example](https://github.com/acwwat/terraform-amazon-bedrock-agent-example) basic example, as `marshal read` records it:

<!-- example: record-terraform -->
```yaml
agent: aws_bedrockagent_agent.forex_asst
platform: bedrock
source:
  format: terraform
  artifact: acwwat-basic.plan.json
  locator: aws_bedrockagent_agent.forex_asst
  detected_by: resource type aws_bedrockagent_agent
spec:
  model:
    requested:
      value: anthropic.claude-3-haiku-20240307-v1:0
      provenance: declared
      basis: aws_bedrockagent_agent.forex_asst.foundation_model
  tools:
  - ref: tool/bedrock.aws_bedrockagent_agent.forex_asst.ForexAPI.GET /currencies/{code}
    name: GET /currencies/{code}
    scope:
      value: read
      provenance: derived
      basis: HTTP GET /currencies/{code}
  permissions:
    identity:
      value: service_principal
      provenance: declared
      basis: 'aws_bedrockagent_agent.forex_asst.agent_resource_role_arn: IAM role aws_iam_role.bedrock_agent_forex_asst'
    principal:
      value: AmazonBedrockExecutionRoleForAgents_ForexAssistant
      provenance: declared
      basis: aws_iam_role.bedrock_agent_forex_asst.name
    grants:
    - target: other/aws.arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-haiku-20240307-v1:0
      privilege: bedrock:InvokeModel
      scope: execute
      provenance: declared
      basis: acwwat-basic.plan.json#aws_iam_role.bedrock_agent_forex_asst
bindings:
  executors:
    tool/bedrock.aws_bedrockagent_agent.forex_asst.ForexAPI.GET /currencies/{code}: function:ForexAPI
# Abbreviated: the other tool, the instructions and the version.
```

## Values known only after apply

A plan doesn't know every value yet: an ARN, an id, or a name with a random prefix is "known after apply". Marshal follows such a value through the plan's configuration to the resource it comes from, across module inputs (`var.x`) and module outputs (`module.m.x`). That is how an agent is linked to its role, an action group to its agent and Lambda function, and a policy to its role.

- A value that comes from a resource Marshal doesn't read, such as `random_string`, is recorded as unknown, with the resource named in the basis.
- An optional attribute left unset is also "known after apply" in a plan, so a value that comes from no resource is treated as absent, not guessed.
- **Locals can't be followed.** A plan's configuration doesn't record what a `local` holds, so a reference that passes through one (common inside modules) isn't linked. The [aws-ia Bedrock module](https://github.com/aws-ia/terraform-aws-bedrock), for example, passes its role name and agent alias through locals, so its role policies and collaborators' aliases aren't linked in a plan. Marshal warns about each value it couldn't follow for this reason and says what that leaves out, such as `role comes from local.agent_role_name, which a Terraform plan doesn't record, so this policy's permissions aren't linked to a role`, so a missing link isn't mistaken for a missing permission.
- A Cloud Control object attribute, such as `guardrail_configuration`, has one expression for the whole object, so its fields known only after apply are recorded as `${known after apply}`: the guardrail is shown as configured, with values to come.

State JSON has every value known and no configuration. There, a value that equals another resource's ARN, id or name in the same state is linked to that resource.

## Credentials

Only allowlisted attributes are read. A value Terraform marks as sensitive is never copied, and variables' values aren't read. Environment variables of AgentCore runtimes are recorded by name.

## In a pipeline

Save the plan in the job that already runs `terraform plan`, then record it:

```console
$ terraform plan -out tfplan
$ terraform show -json tfplan > plan.json
$ marshal collect plan.json --event proposed --environment prod --report comment.md
```

After `terraform apply` succeeds, record the same plan, or the state, as shipped:

```console
$ terraform show -json > state.json
$ marshal collect state.json --event shipped --environment prod
```

With the [GitHub Action](../github-action.md), point `artifacts` at the JSON file. To detect drift, a scheduled job can run `terraform plan -refresh-only -out live`, then `terraform show -json live`, and record the result as `observed`: a refresh-only plan's values are what is live.

## Gaps

- Raw `.tf` files aren't read; only plan and state JSON.
- Writing Terraform isn't supported. To convert an agent defined in Terraform, write CloudFormation or another format.
- Other providers' agents, such as Vertex AI reasoning engines, Databricks or Azure, aren't read from Terraform yet.

## Testing

The golden tests read plans of public examples from [aws-ia/terraform-aws-bedrock](https://github.com/aws-ia/terraform-aws-bedrock) and [acwwat/terraform-amazon-bedrock-agent-example](https://github.com/acwwat/terraform-amazon-bedrock-agent-example). Unlike other fixtures, the plans are committed: `scripts/terraform_fixtures.py` generates them with pinned Terraform and provider versions, answering a plan's few read-only AWS lookups locally, with no AWS account. A unit test holds an agent defined in Terraform and in CloudFormation to the same version.

**Tested on AWS:** on 2026-10-27, the acwwat basic example (with `model_id` set to Amazon Nova Lite) was planned and applied with Terraform in a real AWS account, in `us-east-1`. Marshal read the saved plan and the state after apply as the same agent, with its two OpenAPI tools, its role and grant, and the Lambda function that runs them, and gave both the same version. Recording the plan as `proposed` and the state as `shipped` gives one version with no changes, as a pipeline needs. That test found that a state has Bedrock's defaults filled in where a plan leaves them out, which is why [values equal to Bedrock's defaults](cloudformation.md#bedrock-defaults) are recorded as absent.
