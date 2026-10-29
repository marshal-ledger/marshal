---
title: "Supported agent formats and versions"
description: >-
  Which variants and versions of each agent definition format Marshal reads and writes, what happens with one it
  doesn't know, and how format changes upstream are caught.
---

# Supported formats and versions

One Marshal release reads every variant below at the same time, so teams on different platforms, or on old and new layouts of the same platform, record into the same ledger. Readers are added, not replaced: a release that learns a new layout keeps reading the old one.

| Format | Variants read | Versions | Written as |
|---|---|---|---|
| [Databricks Asset Bundles](databricks.md) | `databricks.yml` bundles with Genie spaces and agent apps | The bundle schema of the Databricks CLI | Not written |
| [AWS CloudFormation and CDK](cloudformation.md) | `AWS::Bedrock::Agent` (Bedrock Agents Classic), `AWS::BedrockAgentCore::Harness` and its gateways, `AWS::BedrockAgentCore::Runtime`; YAML, JSON and `cdk.out` templates | CloudFormation resource types are unversioned; mapped from AWS's published resource schemas | `AWS::Bedrock::Agent` or `AWS::BedrockAgentCore::Harness` |
| [Terraform](terraform.md) | `terraform show -json` of a saved plan or of state, and OpenTofu's equivalent; Bedrock agents from the AWS provider (`aws_bedrockagent_agent`) or the Cloud Control provider (`awscc_bedrock_agent`), and AgentCore runtimes | Tested with Terraform 1.13.4 | Not written |
| [Copilot Studio](copilot-studio.md) | Workspaces from the Visual Studio Code extension (`.mcs.yml` files), and solutions exported with `pac solution export`, zipped or unpacked | Microsoft's published authoring schema, pinned to a commit | A workspace |
| [Agentforce](agentforce.md) | Agent Script (`.agent` files), and legacy Agent Builder metadata (`Bot`, `GenAiPlannerBundle`, `GenAiPlugin`, `GenAiFunction`); an agent keeps its id when it moves from one to the other | Agent Script as in Salesforce's published examples (forcedotcom/sf-skills) | Agent Script |
| [Google ADK Agent Config](adk-config.md) | `root_agent.yaml` and the sub-agent configs it references | Google's published `AgentConfig.json` schema; written configs load in ADK 2.13 | An Agent Config |
| [Vertex AI Agent Engine](vertex-agent-engine.md) | Saved `reasoningEngines.get` and `list` responses, inventory only | The documented API response | Not written |
| [Python agent code](python.md) | OpenAI Agents SDK, LangChain and LangGraph prebuilt agents, Google ADK, CrewAI, and direct calls to the OpenAI, Anthropic, Google GenAI, LiteLLM and Bedrock Runtime APIs, read without running the code | The public APIs of those packages | Not written |
| [ADL](adl.md) | Documents with `adl_spec` 0.1 to 0.3; a newer version is read as 0.3, with a warning | 0.1, 0.2, 0.3 | ADL 0.3 |
| [Open Agent Spec](agent-spec.md) | Agent Spec documents and their referenced components | Any `agentspec_version`; tested against 26.3.1 | Agent Spec 26.3.1 |
| [AGNTCY OASF](oasf.md) | Records with a `schema_version` | Any `schema_version` | OASF 1.1.0 for a new record; a re-written OASF record keeps its own schema version |
| [Marshal file](marshal-file.md) | Hand-written definitions, for agents no reader covers | One version | A Marshal file |

Not read: Copilot Studio's Dataverse source control layout and agents that exist only in managed solutions, raw Terraform `.tf` files and providers other than AWS, and AgentCore CLI projects (read the CloudFormation that `cdk synth` produces). See [how each format is verified](../design.md#formats-and-how-each-is-verified) for how each writer is tested.

## A variant Marshal doesn't know

- **A field the reader doesn't map** is kept under the platform's section of the record (`extensions.<platform>`), or left out. Nothing is guessed: a value the definition doesn't state is recorded as unknown, with the reason.
- **A tool type the reader doesn't recognize** is left out, or recorded with what the definition states about it. Where a reader can tell, such as an unknown AgentCore harness tool type, it prints a warning naming it.
- **A newer version of a versioned format:** an ADL document after 0.3 is read with the 0.3 rules, with a warning. Agent Spec and OASF documents are read field by field, whatever their version.

Writers produce one version of each format, the one in the table. Marshal doesn't write older versions.

## When a format changes upstream

A weekly job compares each format's published definition (schemas, resource types, keywords and package APIs) with a snapshot, and opens an issue in Marshal's repository when one changes. It also runs the golden tests against the latest version of each public example, and opens an issue when they fail. A change is then mapped in a release and listed in the [changelog](https://github.com/marshal-ledger/marshal/blob/main/CHANGELOG.md).

Until that release, a new field in a definition is unmapped, as described above; existing fields keep reading as before.

## Upgrading Marshal

Run the same Marshal release in every pipeline and on the server, and upgrade them together. See [Upgrading](../deployment.md#upgrading).
