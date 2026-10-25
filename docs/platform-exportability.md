# Platform exportability

What each closed platform lets you get out of it as a machine-readable agent definition, and what that means for Marshal's readers. Surveyed in October 2026 against vendor documentation and public examples. Platforms change quickly, so each section lists its sources.

## Summary

| Platform | Best export path | Format | Instructions | Model | Tools | Knowledge | Identity | Starting tier |
|---|---|---|---|---|---|---|---|---|
| Databricks | Asset Bundle in Git | YAML + JSON | Genie: yes. Apps: in code | Apps: in code | App resources | Genie data sources, UC tables | Derivable | **Full** (Genie), **Partial** (Apps) |
| Copilot Studio | VS Code extension clone | YAML folder | Yes | Alias (`modelNameHint`) | Yes (connectors, MCP, flows) | Yes | Per action | **Partial**, Full likely |
| Bedrock Agents | CloudFormation / CDK synth | JSON / YAML | Yes | Yes | Yes (action groups) | By ID | IAM role ARN | **Partial**, Full likely |
| Bedrock AgentCore | CloudFormation | JSON / YAML | In code | In code | In code | In code | IAM role ARN | **Inventory** |
| Vertex Agent Engine | API (`get` / `list`) | JSON | In code | In code | In code | In code | Service account | **Inventory** |
| Vertex ADK Agent Config | `root_agent.yaml` in Git | YAML | Yes | Yes | Yes | Via tools | Not declared | **Partial** |
| Vertex Conversational Agents | Export package | Zipped JSON | Yes | Yes | Yes | Yes | Not declared | **Partial** |
| Agentforce (new Builder) | Metadata API retrieve | Agent Script (`.agent`) + XML | Yes | Not seen in examples | Yes (Flow, Apex, prompt actions) | Via actions | Agent user | **Partial**, Full likely |
| Agentforce (legacy Builder) | Metadata API retrieve | XML | Yes | No | Yes | Via actions | Agent user | **Partial** |

The main finding cuts across platforms. Every vendor now has two kinds of agent:

- **Declarative agents**: Copilot Studio, Bedrock Agents, Genie spaces, Agentforce and Conversational Agents. The whole definition lives in a platform artifact that Marshal can read.
- **Code agents**: Databricks Apps, AgentCore Runtime and Agent Engine. The platform artifact only says how the agent is packaged and what it may access. Model, instructions and tools live in the application code.

For code agents, the platform reader records identity, permissions and deployment. The pro-code readers (LangGraph, OpenAI Agents SDK, CrewAI, and ADK as a candidate) fill in the rest by reading the source repository. That makes the pro-code readers more important than the original plan assumed.

## Databricks

**Export path.** Databricks Asset Bundles (`databricks.yml` plus included files) are the as-code definition. They are already in Git for teams that use them, so the CI collector needs no platform credentials. `databricks bundle generate genie-space` round-trips an existing Genie space into a bundle.

**What a bundle holds** (checked against the bundle JSON Schema in `databricks/cli` v1.22.1):

- `genie_spaces`: title, description, warehouse, permissions, and a serialized space, inline or in a `.geniespace.json` file. The space holds text instructions, example SQL, sample questions and data sources (Unity Catalog tables). Genie spaces deploy only with the direct deployment engine.
- `apps`: the current deployment path for Databricks agent templates (LangGraph, OpenAI Agents SDK, LangChain). An app declares the resources it may use: serving endpoints, Genie spaces, UC functions, tables, volumes, connections, model services, SQL warehouses, jobs, secrets, experiments and Lakebase databases, each with a permission level. Model choice, instructions and tool wiring are in the app's code.
- `model_serving_endpoints`: served entities, AI Gateway guardrails, rate limits. The older agent deployment path. From the bundle alone, an endpoint serving an agent can't be told apart from one serving any other model.
- Also present: `mcp_services`, `model_services` and `model_provider_services` (Unity Catalog AI services), and `vector_search_indexes`.

**Gaps.** For apps, the model, instructions and tools are in code. Environment variable values and target overrides are environment-bound. A UC function's write behavior is not declared.

**Reader.** Implemented: see [readers/databricks.md](readers/databricks.md).

Sources: [databricks/cli bundle schema](https://github.com/databricks/cli/blob/main/bundle/schema/jsonschema.json), [databricks/bundle-examples](https://github.com/databricks/bundle-examples), [databricks/app-templates](https://github.com/databricks/app-templates).

## Microsoft Copilot Studio

**Export paths.**

1. **Copilot Studio extension for VS Code** (generally available). *Clone agent* downloads an agent's full definition as a YAML folder that can be committed to Git. *Apply changes* pushes edits back. Clone is an interactive editor command; no headless CLI is documented. It applies to agents on the standard harness.
2. **Power Platform solutions** (`pac solution export`). Microsoft's recommended path for moving agents between environments. The agent lives in Dataverse as `bot` and `botcomponent` records.
3. **Dataverse Web API.** Reads the same records directly. It suits an optional poller with admin read access.

**What a clone holds.**

```text
agent.mcs.yaml                 # kind: GptComponentMetadata: displayName, instructions,
                               #   conversationStarters, aISettings.model.modelNameHint
topics/*.mcs.yaml              # kind: AdaptiveDialog: conversation flows
actions/*.mcs.yml              # kind: TaskDialog: connectors, MCP servers, REST tools,
                               #   with connectionReference and credential mode
knowledge/                     # knowledge source definitions
workflows/<name>/              # Power Automate flows: metadata.yaml + workflow.json
trigger/*.mcs.yaml             # event and schedule triggers
settings.mcs.yml               # agent settings
connectionreferences.mcs.yml   # connection references (environment-bound)
```

**Gaps.** Files uploaded as knowledge are listed but not downloaded by default. Connection references are environment-bound. The model is an alias hint, not a resolved model. Microsoft's own authoring tools note that the YAML schema may change without notice, so the reader should validate against the published schema and report unknown kinds rather than fail.

**Recommended reader.** Read the cloned YAML folder first, since it is the native definition and maps cleanly to the ledger model. Add the solution zip second, for teams that ship through solutions. Partial tier at first, with Full likely.

Sources: [extension overview](https://learn.microsoft.com/en-us/microsoft-copilot-studio/visual-studio-code-extension-overview), [clone an agent](https://learn.microsoft.com/en-us/microsoft-copilot-studio/visual-studio-code-extension-clone-agent), [edit agent components](https://learn.microsoft.com/en-us/microsoft-copilot-studio/visual-studio-code-extension-edit-agent-components), [microsoft/skills-for-copilot-studio](https://github.com/microsoft/skills-for-copilot-studio) (MIT; includes the authoring schema and example agent files).

## Amazon Bedrock

Bedrock has two agent products, with very different exportability.

### Bedrock Agents

**Export path.** `AWS::Bedrock::Agent` in CloudFormation, or CDK synthesized to CloudFormation (`cdk.out/*.template.json`). For agents built in the console, the Bedrock Agents API (`GetAgent`, `ListAgentActionGroups`, `ListAgentKnowledgeBases`, `ListAgentVersions`, `ListAgentAliases`) returns the same structure.

**What the resource holds.**

- `Instruction` and `FoundationModel` (model ID or inference profile ARN).
- `ActionGroups`: a Lambda executor, plus an OpenAPI schema (in S3 or inline) or a function schema.
- `KnowledgeBases`: by ID.
- `GuardrailConfiguration`, `MemoryConfiguration`, `PromptOverrideConfiguration` and custom orchestration.
- `AgentCollaboration` and `AgentCollaborators`, for multi-agent setups.
- `AgentResourceRoleArn` (the agent's identity) and tags.

Versions and deployments map to `AWS::Bedrock::AgentAlias`.

**Gaps.**

- Action group schemas stored in S3 are references, not content.
- The role's actual permissions sit in separate IAM resources. They can be resolved when they are in the same template.
- Whether an action writes is not declared. The OpenAPI method (`GET` versus `POST`) is a usable signal, recorded as derived.

**Recommended reader.** Read CloudFormation templates and CDK output. Partial tier at first, with Full likely, since the resource is fully declarative.

### Bedrock AgentCore Runtime

**Export path.** `AWS::BedrockAgentCore::Runtime` and `RuntimeEndpoint` in CloudFormation (available since September 2025).

**What the resource holds.**

- The artifact: a container image or code.
- The IAM role, network, authorizer, protocol (`MCP`, `HTTP`, `A2A`, `AGUI`), environment variables, lifecycle and tags.

There is no model, instruction or tool definition. AgentCore hosts code agents built with any framework (Strands, LangGraph, CrewAI and others).

**Recommended reader.** Inventory tier: identity, protocol, deployment and endpoints. Model, instructions and tools come from the pro-code reader for the agent's framework.

Not covered by this survey: AgentCore Gateway, which defines tools for AgentCore agents.

Sources: [AWS::Bedrock::Agent](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-bedrock-agent.html), [AWS::BedrockAgentCore::Runtime](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-bedrockagentcore-runtime.html), [AgentCore CloudFormation announcement](https://aws.amazon.com/about-aws/whats-new/2025/09/amazon-bedrock-agentcore-runtime-browser-code-interpreter-vpc-privatelink-cloudformation-tagging/), [GetAgent](https://docs.aws.amazon.com/bedrock/latest/userguide/bedrock-agent_example_bedrock-agent_GetAgent_section.html).

## Google Vertex AI

Google's agent products are now documented under the Gemini Enterprise Agent Platform. Four paths matter.

1. **Agent Engine** (the `reasoningEngines` resource). Hosts code agents. Its spec records the framework (`google-adk`, `langchain`, `langgraph`, `ag2`, `llama-index`, `custom`) and the packaged code. The REST API has `get`, `list` and `patch`, but no export. Model, instructions and tools are in code. **Inventory** tier through an API poller, with the rest from pro-code readers.
2. **ADK Agent Config.** A declarative `root_agent.yaml` with name, model, description, instruction, tools and sub-agents, loaded by ADK as an `LlmAgent`. It lives in Git and reads statically. **Partial**, and a good ADK entry point. Deploying the same agent to Agent Engine is a separate step, so the two must be linked by source repository.
3. **Managed Agents** (`projects.locations.agents`, v1beta1, pre-GA). Declarative: `system_instruction`, `tools` (built-ins and remote MCP servers), `base_agent` and environment. Readable through `get` and `list`; there is no export file. Worth an inventory poller once it reaches GA.
4. **Conversational Agents / CX Agent Studio** (Dialogflow CX lineage). Exports a JSON package (zip) designed for CI/CD. `environment.json` separates the environment-specific values, which maps directly onto the ledger's bindings. **Partial**.

**Gaps.** The most common production path, Agent Engine, carries the least definition. Identity is a service account, declared at deploy time.

Sources: [Agent Platform API](https://docs.cloud.google.com/vertex-ai/docs/reference/rest), [ReasoningEngineSpec](https://docs.cloud.google.com/gemini-enterprise-agent-platform/reference/rest/v1beta1/ReasoningEngineSpec), [Managed Agents](https://docs.cloud.google.com/gemini-enterprise-agent-platform/build/managed-agents/create-manage), [projects.locations.agents](https://docs.cloud.google.com/gemini-enterprise-agent-platform/reference/rest/v1beta1/projects.locations.agents), [ADK Agent Config](https://adk.dev/agents/config/), [CX Agent Studio export and import](https://docs.cloud.google.com/customer-engagement-ai/conversational-agents/ps/export).

## Salesforce Agentforce

**Export path.** The Metadata API, through the Salesforce CLI.

- `sf project retrieve start --metadata AiAuthoringBundle:<Name>` retrieves the editable source.
- `"AiAuthoringBundle:<Name>_*"` retrieves the immutable snapshot of each published version.
- `Agent:<Name>` retrieves the runtime graph.

The new Builder's types need API version 66.0 or later. `GenAiPlannerBundle` is available from 64.0; older orgs used `GenAiPlanner`.

**What it holds.**

- **AiAuthoringBundle** (new Agentforce Builder): `aiAuthoringBundles/<Name>/<Name>.agent` plus `<Name>.bundle-meta.xml`. The `.agent` file is Agent Script, a declarative, indentation-based language. It declares:
  - system instructions and messages
  - `access.default_agent_user`: the identity the agent runs as
  - `config`: developer name, label and description
  - variables, including values linked from the messaging session
  - a start agent and subagents (topics), each with reasoning instructions
  - actions, with typed inputs and outputs and targets such as `flow://` and `apex://`
  - `available when` guards on actions
- **Runtime graph**, created by publishing: `Bot`, `BotVersion` and `GenAiPlannerBundle`, with `GenAiPlugin` (topics) and `GenAiFunction` (actions). Agents built in the legacy Builder exist only in this form.

**Gaps.**

- Action implementations (Flow, Apex) are separate metadata. Write behavior has to be derived from them.
- The agent user's permissions are in permission sets.
- No model setting appeared in the Agent Script examples reviewed.
- `GenAiPlannerBundle`, `AiAuthoringBundle` and `GenAiFunction` don't support SOQL, so a poller must use metadata retrieve. `BotDefinition` and `BotVersion` do support SOQL.

**Recommended reader.** Agent Script first: it is declarative, versioned and carries identity. It needs a small parser, since the format is not YAML. Fall back to `GenAiPlannerBundle` XML for legacy agents. Partial tier at first, with Full likely for Agent Script. The original plan put Agentforce at Inventory; this is better than that.

Sources: [Author an agent](https://developer.salesforce.com/docs/ai/agentforce/guide/agent-dx-nga-author-agent.html), [The New Agentforce Metadata and Development Lifecycle](https://developer.salesforce.com/blogs/2026/05/new-agentforce-metadata-and-development-lifecycle), [Trailhead: Author Your Agent Using Agent Script](https://trailhead.salesforce.com/content/learn/projects/create-an-agent-using-pro-code-tools/create-an-agent-from-your-dx-project), [forcedotcom/sf-skills](https://github.com/forcedotcom/sf-skills) (Apache-2.0; includes example `.agent` files and a CLI reference).

## Golden fixtures by platform

| Platform | Public source | License |
|---|---|---|
| Databricks | `databricks/bundle-examples`, `databricks/app-templates` | Databricks License (fetched at a pinned commit, not vendored) |
| Copilot Studio | `microsoft/skills-for-copilot-studio` | MIT |
| Agentforce | `forcedotcom/sf-skills` | Apache-2.0 |
| Bedrock | `aws-samples` agent repositories | Varies by repository (MIT-0 is common) |
| Vertex | `google/adk-samples` | Apache-2.0 |
