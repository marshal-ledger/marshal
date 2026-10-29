---
title: "AgentCore harness inventory from AgentCore CLI projects"
description: >-
  How Marshal reads AgentCore harnesses from AgentCore CLI projects (agentcore.json with harness.yaml or harness.json) for an AI agent inventory, in CI before agentcore deploy.
---

# AgentCore CLI projects: AgentCore harnesses

Format id: `agentcore-cli`. Reads the harnesses of a project made with the [AgentCore CLI](https://github.com/aws/agentcore-cli) (`agentcore create`, `agentcore add harness`). Marshal never runs the CLI or deploys anything: it reads the project's files.

| Files | Platform id | Tier |
|---|---|---|
| `agentcore/agentcore.json` (`harnesses: [{name, path}]`), and in each harness folder `harness.yaml` or `harness.json`, with `system-prompt.md` | `bedrock-agentcore` | Read in full, as for CloudFormation |

Point Marshal at the project folder, at `agentcore/agentcore.json`, or at a folder above: every `agentcore/agentcore.json` below it is read. A project that defines runtimes (agent code) and no harness is left to the [Python](python.md) reader.

The agent id is the project's name and the harness's name, such as `thepulse/thepulse`, so it stays the same wherever the repository is checked out.

## Two file shapes

- **`harness.yaml`**, in projects of schema version 2 (the current CLI): the Harness API's nesting in camelCase, such as `model.bedrockModelConfig.modelId`.
- **`harness.json`**, in version 1 projects: the older flat shape, such as `model: {provider: bedrock, modelId: ...}` and `sessionStoragePath`.

Marshal reads both, translating `harness.json` the way `agentcore migrate` does, so a harness migrated from one to the other keeps its version. The instructions are the inline `systemPrompt` if the file sets one, else `system-prompt.md` next to it, as the CLI deploys them.

## Same mapping as CloudFormation

`agentcore deploy` turns each harness into an `AWS::BedrockAgentCore::Harness`. Marshal translates the harness file into that resource's properties and reads it with the [CloudFormation](cloudformation.md#agentcore-harnesses) mapping, so a harness gets the same record and version whether it is defined in a CLI project, CloudFormation or [Terraform](terraform.md). Provenance names the file and key, such as `model.modelId` in `app/thepulse/harness.json`, or the prompt file.

What that mapping records, briefly:

- **Model:** the model id, its provider (from the model configuration, or the model id), and temperature, top-p, top-k and max tokens. An API key credential (`apiKeyArn`) goes in `bindings`.
- **Tools:** remote MCP servers by URL, gateways by ARN, the browser and code interpreter, and inline functions with their input schema. The default `shell` and `file_operations` tools are recorded unless `allowedTools` leaves them out, so a harness that can run commands shows as one.
- **Identity:** `executionRoleArn` when set. Without it, `agentcore deploy` creates the role, so the identity is recorded as a service role (`derived`) and its name as unknown.
- **Everything else** (memory, skills, truncation, environment, limits, `allowedTools`, the caller authorizer) goes in `extensions.bedrock-agentcore`, as for CloudFormation. Two settings only the CLI has are kept there too: `dockerfile` and `connections`. Tags and the VPC id used for container builds aren't recorded.

`networkMode: PUBLIC` with no VPC settings is the CLI's default, so it is recorded as absent: a file that states it and one that omits it give the same version.

The public [aws-samples/sample-AgentCore-Managed-Harness-News](https://github.com/aws-samples/sample-AgentCore-Managed-Harness-News) project, as `marshal read` records it:

<!-- example: record-agentcore-cli -->
```yaml
agent: thepulse/thepulse
platform: bedrock-agentcore
source:
  format: agentcore-cli
  artifact: app/thepulse/harness.json
  locator: harness thepulse
  detected_by: harness registered in agentcore/agentcore.json
spec:
  model:
    requested:
      value: global.anthropic.claude-sonnet-4-6
      provenance: declared
      basis: model.modelId
  instructions:
    provenance: declared
    basis: app/thepulse/system-prompt.md
  tools:
  - ref: tool/bedrock-agentcore.browser
    name: browser
    scope:
      value: execute
      provenance: derived
      basis: AgentCore Browser operates a web browser
  - ref: tool/bedrock-agentcore.shell
    name: shell
    scope:
      value: execute
      provenance: derived
      basis: runs bash commands in the session's microVM; AllowedTools is not set, so every session has
        it
  - ref: tool/bedrock-agentcore.file_operations
    name: file_operations
    scope:
      value: write
      provenance: derived
      basis: views, creates and edits files in the session's filesystem; AllowedTools is not set, so every
        session has it
  permissions:
    identity:
      value: service_principal
      provenance: derived
      basis: executionRoleArn is not set, so agentcore deploy creates an execution role
  extensions:
    bedrock-agentcore:
      memory:
        AgentCoreMemoryConfiguration:
          Name: thepulseMemory
      max_iterations: 75
      timeout_seconds: 1800
# Abbreviated: the instructions' text and hash, and the version.
```console
$ marshal collect . --event proposed --environment prod --report comment.md   # in the pull request
$ agentcore deploy
$ marshal collect . --event shipped --environment prod                         # after the deploy succeeds
```

With the [GitHub Action](../github-action.md), point `artifacts` at the project folder.

## Gaps

- A gateway declared in the project's `agentCoreGateways` isn't expanded into its tools: a harness names a gateway by ARN, so the gateway is recorded as one tool. Gateways defined in the same CloudFormation template are expanded.
- A memory named in the project (`memory.agentCoreMemoryConfiguration.name`) is recorded by name; its strategies in `agentcore.json` aren't added to the record.
- The project's runtimes, evaluators, policy engines and other resources aren't read. The CDK app the CLI generates (`agentcore/cdk`) isn't read either; read its `cdk synth` output with the CloudFormation reader if needed.
- Writing a CLI project isn't supported. The [`agentcore-harness`](cloudformation.md#writing-an-agentcore-harness) writer writes the same harness as CloudFormation; a memory named in the project becomes a template parameter for its ARN.

## Testing

The golden tests read three public projects, pinned to a commit: [aws-samples/sample-AgentCore-Managed-Harness-News](https://github.com/aws-samples/sample-AgentCore-Managed-Harness-News) (MIT-0), [Capitano00/3D-RAMS](https://github.com/Capitano00/3D-RAMS) (MIT, ten harnesses) and [aryankhanna2004/ppt-agent-agentcore](https://github.com/aryankhanna2004/ppt-agent-agentcore) (MIT). All three are version 1 projects with `harness.json`; no public project with `harness.yaml` was found yet, so unit tests cover that shape with the examples from the CLI's harness configuration guide. A unit test holds a harness defined in a CLI project and in CloudFormation to the same record and version, and another holds `harness.json` and `harness.yaml` to the same version.

Not yet tested against a deployed harness.
