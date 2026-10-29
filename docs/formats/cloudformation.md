---
title: "Bedrock and AgentCore agent inventory from AWS CloudFormation and CDK"
description: >-
  How Marshal reads Bedrock Agents and AgentCore harnesses and runtimes from CloudFormation and CDK templates, and writes CloudFormation for Bedrock Agents and the AgentCore harness.
---

# AWS CloudFormation: Bedrock Agents and AgentCore

Format id: `cloudformation`. Reads CloudFormation templates (YAML with short-form tags, or JSON) and CDK output (`cdk.out/*.template.json`). Two writers produce CloudFormation: `cloudformation` writes one `AWS::Bedrock::Agent`, and `agentcore-harness` writes one `AWS::BedrockAgentCore::Harness`.

| Resource | Platform id | Tier |
|---|---|---|
| `AWS::Bedrock::Agent` | `bedrock` | Partial: read in full; written as the agent resource without its surrounding stack |
| `AWS::BedrockAgentCore::Harness` | `bedrock-agentcore` | Partial: read in full, with the tools of gateways in the same template; written as the harness without its surrounding stack |
| `AWS::BedrockAgentCore::Runtime` | `bedrock-agentcore` | Inventory: identity, grants, protocol, artifact, authorizer; model and tools are in the runtime's code |

Bedrock Agents has been in maintenance mode since July 30, 2026: accounts without Bedrock Agents use in the past year can't create agents, and AWS recommends the AgentCore harness. See [Move Bedrock agents to the AgentCore harness](../guides/bedrock-agents-to-agentcore.md).

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

### Bedrock defaults

A value equal to what Bedrock stores when an agent doesn't set it is recorded as absent: an idle session timeout of 600 seconds, agent collaboration `DISABLED` and orchestration type `DEFAULT`. These are the values real deploys produced with nothing set. An agent then records the same version whether its source states a default or leaves it out, and a [Terraform](terraform.md) plan, which leaves unset values out, agrees with the state after apply, which has them filled in.

Action verbs map to scopes as follows: `Get`, `List`, `Describe`, `Retrieve`, `Query`, `Search`, `Scan` and `Read` are read; `Invoke`, `Execute`, `Start`, `Run`, `Converse`, `Send` and `Publish` are execute; other verbs are write; `*` is admin. Managed policies are listed by ARN but not expanded into grants.

## AgentCore harnesses

A harness is AgentCore's declarative agent: AgentCore runs the agent loop, and the template states the model, system prompt and tools.

| Ledger field | Source | Provenance |
|---|---|---|
| `display_name` | `HarnessName` | declared / derived |
| `model.requested` | `ModelId` in the model configuration (`BedrockModelConfig`, `OpenAiModelConfig`, `GeminiModelConfig` or `LiteLlmModelConfig`) | declared / derived |
| `model.provider` | The vendor in a Bedrock model id; `openai` or `google` from the configuration's type | derived |
| `model.parameters` | `Temperature`, `TopP`, `TopK`, `MaxTokens` | declared |
| `instructions` | The `SystemPrompt` blocks, joined | declared |
| `tools[]` | `remote_mcp` → `tool/mcp.<url>`; `inline_function`, with its description and input schema; `agentcore_browser`; `agentcore_code_interpreter` | declared |
| `tools[]` through a gateway | `agentcore_gateway`: when the gateway is in the template, each of its targets' tools (Lambda tool schemas, MCP server endpoints, inline OpenAPI operations), with the Lambda's role as their scope; otherwise one tool for the gateway | declared / derived |
| `tools[]` built-in | `shell` (execute) and `file_operations` (write), which every session has unless `AllowedTools` restricts them | derived |
| `tools[]` from the model | Tools the model provider runs, listed in the model's `AdditionalParams.tools`, such as OpenAI's `web_search` | derived |
| `permissions.identity`, `principal`, `grants[]` | `ExecutionRoleArn`, resolved as for Bedrock Agents | declared |
| `extensions.bedrock-agentcore` | Guardrail (the model's `guardrailConfig`), allowed tools, memory, hooks, skills, environment, truncation, authorizer, limits, gateway outbound auth, environment variable and MCP header names (never values), endpoints | declared |
| `bindings` | The Lambda that runs each gateway tool, as for Bedrock action groups; the model's API key ARN | declared |

## AgentCore runtimes

The record holds the runtime name and description, the execution role's identity and grants, protocol (`MCP`, `HTTP`, `A2A`, `AGUI`), artifact (container or code), network and authorizer configuration, environment variable names (never values), and endpoints. Model, instructions and tools are unknown: read the runtime's source with the [Python reader](python.md).

## Writing

**Tested:** every loss in converting every fixture is explained, the agent keeps its identity, and every written template passes [cfn-lint](https://github.com/aws-cloudformation/cfn-lint), AWS's CloudFormation linter. The one exception is an instruction shorter than Bedrock's 40-character minimum, which the writer reports as a warning rather than padding. Since cfn-lint 1.59, every template also gets a warning (W3697) that Bedrock Agents is in maintenance mode (cfn-lint dates it 2026-06-30; AWS's announcement says July 30, 2026); it is about the service, not the template, so the tests set it aside.

**Deployed:** on 2026-10-27, a template the writer produced, for an agent with three functions (one requiring confirmation) and a role, deployed unchanged to Amazon Bedrock in `us-east-1` with Amazon Nova Lite. The agent was prepared, and the name, model, instructions, functions, parameters and confirmation settings Bedrock stored matched the source. A function whose confirmation the source doesn't state is stored as not needing confirmation, so the writer warns about each one.

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

## Writing an AgentCore harness

Format id: `agentcore-harness`. **Tested:** every loss in converting every fixture is explained, the agent keeps its identity, and every written template passes cfn-lint against AWS's published harness schema. Not yet deployed to AgentCore.

The writer emits one `AWS::BedrockAgentCore::Harness`:

- **Role:** the `ExecutionRoleArn` template parameter, unless the record's principal is a role ARN.
- **Model:** `BedrockModelConfig` for Bedrock model ids; `OpenAiModelConfig` or `GeminiModelConfig` for those providers, with the API key's credential provider as a template parameter. With no model in the source, `ModelId` is a template parameter and the conversion warns.
- **Tools:** MCP servers with a URL as `remote_mcp`, gateways as `agentcore_gateway`, the code interpreter and browser as their harness tools, and any other tool as an inline function, which returns control to the calling application. A tool that ran in Lambda in the source is named in a warning.
- **Allowed tools:** unless the source has both of the default `shell` and `file_operations` tools, `AllowedTools` lists the agent's own tools, so the harness never gets a shell its source didn't have.
- **Guardrail:** a Bedrock agent's guardrail becomes the model's `guardrailConfig`.
- **Native extensions:** memory, hooks, skills, environment, truncation, authorizer and limits are written back. A setting that depended on a condition in the source template is left out, with a note.
- **Identity:** the agent id, platform and any foreign tool refs go in the resource's Marshal metadata. `marshal convert --migrate` leaves the platform out, so the harness is recorded on `bedrock-agentcore`.

### Declared losses

| Field | Reason |
|---|---|
| `description` | A harness has no description; it becomes the template's |
| `permissions.*` | IAM permissions live on the execution role, which the writer references but doesn't create |
| `tools[].scope`, `reversible`, `output_schema` | Harness tool definitions declare none of these |
| `tools[].requires_confirmation` | A harness has no confirmation setting: the application asks before running an inline function, or a `before_tool_call` hook does |
| `tools[agent/*]` | Another agent is reached through a gateway or MCP server, which the writer doesn't create |
| `knowledge[]` | Knowledge bases are reached through a gateway target or a retrieval tool, which the writer doesn't create |
| `model.provider`, `model.resolved` | The provider follows from the model configuration; the template names the requested model only |
