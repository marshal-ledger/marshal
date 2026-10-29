---
title: "Move Bedrock agents to the AgentCore harness"
description: >-
  Convert an Amazon Bedrock Agents CloudFormation template to an AgentCore harness template, see exactly what
  doesn't carry over, and keep the agent's history in the inventory. No AWS credentials and no AI.
---

# Move Bedrock agents to the AgentCore harness

On July 30, 2026, AWS put Amazon Bedrock Agents in maintenance mode and renamed it Bedrock Agents Classic. Existing agents keep working, but AWS accounts with no Bedrock Agents use in the past year can't create agents, Classic gets no new models, and AWS recommends moving to the AgentCore harness: a declarative agent with a model, a system prompt and tools, which AgentCore runs ([AWS announcement](https://docs.aws.amazon.com/bedrock/latest/userguide/agents-classic-maintenance-mode.html)).

`marshal convert` writes a Bedrock agent's CloudFormation as an `AWS::BedrockAgentCore::Harness` template, and reports every field that doesn't carry over. It works from the template in your repository: it needs no AWS credentials and no access to the account, and it uses no AI, so the same template always gives the same result.

## Convert a template

```console
$ marshal convert cfn/hr-resources.yml --to agentcore-harness --migrate > hr-agent.harness.yaml
```

`--migrate` moves the agent to AgentCore in the ledger: the harness keeps the agent's id, so its history continues, and it is recorded on the `bedrock-agentcore` platform. Without it, the written harness reads back as the same Bedrock agent, which is what a fidelity check wants.

This is the public [HR assistant](https://github.com/aws-samples/amazon-bedrock-samples) from AWS's samples. The report goes to stderr:

<!-- example: convert-hr-to-agentcore -->
```
warning: hr-resources.yml: HRAgent: action group employee-timeoff-database: OpenAPI schema is in S3 and was not read
warning: hr-resources.yml: HRAgent: action group generate-image: OpenAPI schema is in S3 and was not read
warning: hr-resources.yml: HRAgent: action group send-email: OpenAPI schema is in S3 and was not read
warning: hr-resources.yml: HRAgent: action group send-slack-message: OpenAPI schema is in S3 and was not read
hr-resources/HRAgent → agentcore-harness
  Fidelity: 19 lossless · 14 dropped · 5 defaulted
    - description (a harness has no description; it is written as the template's description)
    - tools[tool/bedrock.hr-resources.HRAgent.employee-timeoff-database].scope (harness tool definitions don't declare a scope)
    - tools[tool/bedrock.hr-resources.HRAgent.generate-image].scope (harness tool definitions don't declare a scope)
    - tools[tool/bedrock.hr-resources.HRAgent.send-email].scope (harness tool definitions don't declare a scope)
    - tools[tool/bedrock.hr-resources.HRAgent.send-slack-message].scope (harness tool definitions don't declare a scope)
    - permissions.principal (the execution role is a template parameter, set at deploy time)
    - permissions.grants[other/aws.* lambda:invokeFunction] (IAM permissions live on the harness's execution role, which this writer references but does not create)
    - permissions.grants[other/aws.* bedrock:InvokeModel] (IAM permissions live on the harness's execution role, which this writer references but does not create)
    - permissions.grants[other/aws.* bedrock:Retrieve] (IAM permissions live on the harness's execution role, which this writer references but does not create)
    - permissions.grants[other/aws.* s3:GetObject] (IAM permissions live on the harness's execution role, which this writer references but does not create)
    - extensions.bedrock.* (2 fields; Bedrock Agents-specific; no harness equivalent)
    - extensions.bedrock.* (1 field; a harness has one system prompt; Bedrock Agents' per-stage prompt overrides have no equivalent)
    - extensions.bedrock.* (1 field; harness sessions are managed by AgentCore; Bedrock Agents' idle session timeout doesn't apply)
    + tools[tool/bedrock.hr-resources.HRAgent.employee-timeoff-database].input_schema (an inline function requires an input schema; an empty one was written)
    + tools[tool/bedrock.hr-resources.HRAgent.generate-image].input_schema (an inline function requires an input schema; an empty one was written)
    + tools[tool/bedrock.hr-resources.HRAgent.send-email].input_schema (an inline function requires an input schema; an empty one was written)
    + tools[tool/bedrock.hr-resources.HRAgent.send-slack-message].input_schema (an inline function requires an input schema; an empty one was written)
    + extensions.bedrock-agentcore.allowed_tools (written so the harness gets the default shell and file tools only where the source has them)
  warning: employee-timeoff-database, generate-image, send-email, send-slack-message ran in Lambda in the source and are written as inline functions, which the calling application runs; to keep the Lambda, put it behind an AgentCore gateway
```

The written harness, abbreviated:

<!-- example: harness-hr -->
```yaml
AWSTemplateFormatVersion: '2010-09-09'
Description: HR Agent.
Parameters:
  ExecutionRoleArn:
    Type: String
    Description: ARN of the IAM role the harness runs as
Resources:
  HRAgent:
    Type: AWS::BedrockAgentCore::Harness
    Metadata:
      io.github.marshal-ledger:
        agent: hr-resources/HRAgent
        tool_refs:
          employee-timeoff-database: tool/bedrock.hr-resources.HRAgent.employee-timeoff-database
    Properties:
      HarnessName: HRAgent
      ExecutionRoleArn:
        Ref: ExecutionRoleArn
      Model:
        BedrockModelConfig:
          ModelId: amazon.titan-text-premier-v1:0
      SystemPrompt:
      - Text: You are an AI assistant specializing in HR-related inquiries. Your role is to help employees
          with questions about time off, pay, retirement and other HR topics. You can also generate images
          and send meeting invites.
      Tools:
      - Type: inline_function
        Name: employee-timeoff-database
        Config:
          InlineFunction:
            Description: This action group contains tool to execute statements on the database to get
              or update time off balance.
            InputSchema:
              type: object
              properties: {}
      AllowedTools:
      - employee-timeoff-database
      - generate-image
      - send-email
      - send-slack-message
# Abbreviated: 3 more inline functions.
```

Review it before deploying:

- **Execution role.** The role is a template parameter: point it at a role with `bedrock:InvokeModel` and anything the tools need.
- **Tools.** The agent's action groups ran in Lambda. They are written as inline functions, which return control to the calling application, so the warning above names them. To keep them in Lambda, put each function behind an [AgentCore gateway](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway.html) as a Lambda target and replace the inline functions with one `agentcore_gateway` tool. The HR assistant's OpenAPI schemas are in S3, which the template doesn't include, so the functions have empty input schemas.
- **Default tools.** Every harness session has a `shell` and a `file_operations` tool unless `AllowedTools` restricts them. A Bedrock agent had neither, so the writer lists the agent's own tools in `AllowedTools`.
- **Confirmation.** A function that required confirmation in Bedrock has no equivalent setting in a harness. The report names each one: ask the user in the application before running the inline function, or add a `before_tool_call` hook that does.

## What carries over

| Bedrock Agents | AgentCore harness |
|---|---|
| `FoundationModel` | `Model.BedrockModelConfig.ModelId` |
| `Instruction` | `SystemPrompt` |
| `AgentResourceRoleArn` | `ExecutionRoleArn` |
| Function and OpenAPI action groups | Inline functions, which the application runs; or Lambda targets on an AgentCore gateway, set up by hand |
| `AMAZON.CodeInterpreter` | The `agentcore_code_interpreter` tool |
| `AMAZON.UserInput` | Nothing: the model asks in its reply |
| `GuardrailConfiguration` | The model's `guardrailConfig`, applied to every model call |
| Knowledge bases | Not written: connect them through an AgentCore gateway target or a retrieval tool |
| Prompt overrides, collaborators, custom orchestration | Not written: a harness has one system prompt and runs its own loop |

Each row is in the report when it applies to the agent, with the reason. The table follows AWS's own [comparison](https://docs.aws.amazon.com/bedrock/latest/userguide/agents-classic-maintenance-mode.html#agents-maintenance-mode-comparison).

## Review the move in a pull request

Commit the harness template next to the Bedrock one, or in its place, and the [GitHub Action](../github-action.md) or [GitLab CI](../gitlab-ci.md) template records it as a proposed version of the same agent. The pull request comment lists what changed between the shipped Bedrock agent and the proposed harness, ranked by risk, so a reviewer sees a removed guardrail, a tool that now runs in the application, or the shell tool, before the harness ships.

## Inventory harnesses

Marshal also reads harnesses that teams write themselves, from CloudFormation and CDK output: the model and its provider, the system prompt, the tools (MCP servers, gateway tools, inline functions, the browser and the code interpreter), the execution role and its grants, memory, hooks and limits. A harness that doesn't restrict `AllowedTools` is recorded with its `shell` and `file_operations` tools, so an agent that can run commands shows up as one. See [CloudFormation](../formats/cloudformation.md#agentcore-harnesses).

## Limits

- The writer hasn't been deployed to AgentCore yet. Its output passes cfn-lint against AWS's published harness schema.
- It writes the harness, not its surroundings: the execution role, gateways, gateway targets and memory are referenced, not created.
- AgentCore CLI projects (`agentcore/agentcore.json` and `harness.json`) aren't read yet. Read the CloudFormation that `cdk synth` produces from them.
