# AWS CloudFormation: Bedrock Agents and AgentCore

Format id: `cloudformation`. Reads CloudFormation templates (YAML with short-form tags, or JSON) and CDK output (`cdk.out/*.template.json`). Writes a template containing one `AWS::Bedrock::Agent`.

| Resource | Platform id | Tier |
|---|---|---|
| `AWS::Bedrock::Agent` | `bedrock` | Partial: read in full; written as the agent resource without its surrounding stack |
| `AWS::BedrockAgentCore::Runtime` | `bedrock-agentcore` | Inventory: identity, grants, protocol, artifact, authorizer; model and tools are in the runtime's code |

The agent id is `<template path without extension>/<logical id>`.

## Intrinsic functions

- **`Ref` to a parameter** uses the parameter's default. A value built this way is recorded as derived, with a basis naming the parameter, because a deploy can override it.
- **`Ref` and `GetAtt` to a resource** in the same template are followed. This is how roles, Lambdas, knowledge bases and aliases are linked.
- **`Fn::Sub` and `Fn::Join`** are resolved where their parts are known.
- **`Fn::ImportValue`** becomes `${import:<export name>}`, so two agents importing the same export share the entity.
- **`Fn::If`** takes the branch selected by the template's `Conditions`, evaluated with parameter defaults, and the record notes the condition. When a condition can't be decided, both branches are included.

Anything unresolved stays as a `${...}` token, and the field is recorded as unknown ("set at deploy time").

## Bedrock Agents: read mapping

| Ledger field | Source | Provenance |
|---|---|---|
| `display_name`, `description` | `AgentName`, `Description` | declared / derived |
| `model.requested` | `FoundationModel` | declared / derived |
| `model.provider` | The vendor in the model id or inference profile (`anthropic.`, `us.anthropic.`, ...) | derived |
| `instructions` | `Instruction` | declared / derived |
| `tools[]` | Action groups: inline OpenAPI operations (scope from the HTTP method), function schemas (with `RequireConfirmation`), or one tool per group when the schema is in S3 | declared / derived |
| `tools[].scope` | When the group's executor Lambda is in the template: the widest action in that Lambda's IAM role | derived |
| `tools[]` built-in | `AMAZON.UserInput` → `tool/bedrock.user_input`; `AMAZON.CodeInterpreter` → `tool/bedrock.code_interpreter` | derived |
| `tools[]` agents | Collaborators whose alias is in the template, as `agent/<agent id>` | declared |
| `knowledge[]` | `KnowledgeBases`, with name and type from the knowledge base resource when it is in the template | declared |
| `permissions.identity`, `principal` | `AgentResourceRoleArn`: the role's name, or the ARN | declared |
| `permissions.grants[]` | Allow statements in the role's inline policies and in `AWS::IAM::Policy` resources attached to it; scope from the action verb | declared |
| `extensions.bedrock` | Guardrail, memory, prompt overrides, orchestration, collaboration mode, aliases, disabled action groups, S3 schema locations, external collaborators, managed policy ARNs, conditions | declared |
| `bindings.executors` | The Lambda function that runs each action group's tools, by tool ref: a literal function ARN, `import:<export>` for an imported one, or `function:<name>` when its name is stated. Used to match agents in other stacks that share it; see [Resolving refs across platforms](../ledger-model.md#resolving-refs-across-platforms) | declared |

Action verbs map to scopes as follows: `Get`, `List`, `Describe`, `Retrieve`, `Query`, `Search`, `Scan` and `Read` are read; `Invoke`, `Execute`, `Start`, `Run`, `Converse`, `Send` and `Publish` are execute; other verbs are write; `*` is admin. Managed policies are listed by ARN but not expanded into grants.

## AgentCore runtimes

The record holds the runtime name and description, the execution role's identity and grants, protocol (`MCP`, `HTTP`, `A2A`, `AGUI`), artifact (container or code), network and authorizer configuration, environment variable names (never values), and endpoints. Model, instructions and tools are unknown: read the runtime's source with the [Python reader](python.md).

## Writing

**Tested:** every loss in converting every fixture is explained, the agent keeps its identity, and every written template passes [cfn-lint](https://github.com/aws-cloudformation/cfn-lint), AWS's CloudFormation linter. The one exception is an instruction shorter than Bedrock's 40-character minimum, which the writer reports as a warning rather than padding. The template hasn't yet been deployed to an AWS account.

References the source template couldn't resolve are written back as CloudFormation: an imported value as `Fn::ImportValue`, and an attribute of a resource in the source stack, such as a guardrail's id, as a template parameter to fill in when deploying.

The writer emits one `AWS::Bedrock::Agent`:

- **Role:** the `AgentResourceRoleArn` template parameter, unless the record's principal is a role ARN.
- **Tools:** functions in a `RETURN_CONTROL` action group, executed by the calling application. Built-in tools map back to their parent signatures.
- **Knowledge:** Bedrock knowledge bases, attached by id.
- **Preparation:** `AutoPrepare` is set, as in the AWS samples, so the agent is ready to test once the stack is deployed.
- **Native extensions:** guardrail, memory, orchestration and prompt overrides are written back.
- **Identity:** the agent id, platform and any foreign tool refs go in the resource's Marshal metadata.

The conversion prints a warning when the model isn't a Bedrock model id, or when the instructions are shorter than Bedrock's 40-character minimum.

### Declared losses

| Field | Reason |
|---|---|
| `permissions.*` | IAM permissions live on the role, which the writer references but doesn't create |
| `tools[].scope`, `reversible`, `output_schema` | Bedrock function definitions declare none of these |
| `tools[agent/*]` | Collaborators are referenced by alias ARN, which exists only after deploy |
| `knowledge[]` other than Bedrock knowledge bases | Only knowledge bases can be attached, by id |
| `model.parameters`, `model.provider`, `model.resolved` | Not part of the agent resource |
