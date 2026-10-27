---
title: "Copilot Studio agent inventory from solutions and YAML"
description: >-
  How Marshal reads Microsoft Copilot Studio agents from exported solutions and workspace YAML, including whose credentials each tool uses, and writes workspace YAML.
---

# Microsoft Copilot Studio

Format id: `copilot-studio`. Reads two layouts of the same definitions:

- **Workspaces** that the Copilot Studio extension for Visual Studio Code clones and applies: a folder of `.mcs.yml` files. The writer produces this layout.
- **Solutions** exported with `pac solution export`, as the zip or unpacked with `pac solution unpack`. Exporting needs no access to the agent beyond the Power Platform CLI, so a [scheduled job](../../examples/scheduled/) can record agents built in the web designer.

Tests validate written files against the Copilot Studio authoring schema, by each file's `kind`.

```console
$ marshal read path/to/cloned-agent
$ marshal read Contoso_1_0_0_1.zip
$ marshal convert agent.adl.yaml --to copilot-studio --out converted/
```

## Workspace layout

| File | Read as |
|---|---|
| `agent.mcs.yml` | Instructions, conversation starters, model (`aISettings.model`), capabilities (`gptCapabilities`) |
| `settings.mcs.yml` | Schema name (the agent id), display name, access control policy, authentication mode |
| `actions/*.mcs.yml` | Tools: connectors, MCP connectors, Power Automate flows, connected agents, AI Builder models |
| `connectionreferences.mcs.yml` | The connector behind each connection reference |
| `knowledge/*.mcs.yml`, `knowledge/files/` | SharePoint, public website and other search sources; uploaded files |
| `agents/<Name>/agent.mcs.yml` | Sub-agents, each recorded as its own agent |
| `skills/`, `topics/`, `trigger/` | Bot Framework skills, topic names and triggers, HTTP requests made from topics, autonomous triggers |

Display names come from the files' `# Name:` comment headers where present. A file wrapped in a Markdown code fence is still read, and so is one with unquoted `@` keys such as `@odata.type`, which Copilot Studio writes but YAML reserves.

## Solution layout

| Path | Read as |
|---|---|
| `bots/<schema>/bot.xml` | Schema name (the agent id), display name, access control policy, authentication mode and trigger. These are stored as numbers and read as the names a workspace uses. |
| `bots/<schema>/configuration.json` | The rest of the settings, which a workspace keeps in `settings.mcs.yml` |
| `botcomponents/<schema>/data` | One component's YAML, the same content as the workspace file of the same `kind`: `GptComponentMetadata` as `agent.mcs.yml`, `TaskDialog` as an action, `AgentDialog` as a sub-agent, `AdaptiveDialog` as a topic, `KnowledgeSourceConfiguration` as knowledge, `ExternalTriggerConfiguration` as a trigger |
| `botcomponents/<schema>/botcomponent.xml` | The component's display name and parent agent; file attachments (component type 14) as uploaded knowledge files |
| `Other/Customizations.xml` (`customizations.xml` in a zip) | The connector behind each connection reference |

A solution can hold several agents; each component belongs to the agent its `parentbotid` names. From a zip, only `bots/`, `botcomponents/` and the customizations file are extracted, to a temporary folder, and entries with absolute or parent-directory paths are skipped.

## Read mapping

| Ledger field | Source | Provenance |
|---|---|---|
| agent id | `settings.mcs.yml` `schemaName` (sub-agents: `<schema name>/<folder>`) | |
| `instructions` | `agent.mcs.yml` `instructions` (sub-agents: `settings.instructions`) | declared |
| `model.requested`, `provider` | `aISettings.model.modelNameHint` and `provider`; GPT hints → `azure-openai`, Claude hints → `anthropic` | declared / derived |
| `tools[]` | Connector actions as `tool/powerplatform.<connector>.<operation>`; MCP connectors as `tool/mcp.powerplatform.<connector>`; flows as `tool/powerplatform.flow.<id>`; prompts as `tool/powerplatform.ai_plugin.<name>`; connected agents and sub-agents as `agent/<id>`; `webBrowsing` and `codeInterpreter` capabilities; HTTP requests, connector operations and flows that topics call | declared / derived |
| `tools[].input_schema` | Inputs the model fills in (`AutomaticTaskInput`) | declared |
| `tools[].scope`, `reversible` | For standard Microsoft connector operations (Office 365 Users and Outlook, Dataverse, Teams, MSN Weather), from Microsoft's connector reference: reads are `read` and reversible; writes are `write`; operations that send a message are also irreversible. The table is [`powerplatform-operations.yaml`](../../src/marshal_ledger/readers/powerplatform-operations.yaml). Other operations, and ones whose effect depends on their input such as `HttpRequest`, stay unknown. | derived |
| `tools[].requires_confirmation` | An action's `confirmation` prompt | declared |
| `knowledge[]` | Sites as `knowledge/<url>`, so a SharePoint site shared with another agent is the same entity; uploaded files; scope read | declared / derived |
| `permissions.identity` | Derived from the connection mode of every action, topic call and trigger: all `Invoker` → `on_behalf_of_user`; all `Maker` (the schema default) → `shared_credential`; both → `mixed`. A flow's connections are defined in the flow, not the agent, so an agent that calls flows is `unknown` unless its other connections already show both modes. | derived |
| `permissions.access[]` | `accessControlPolicy`; `authenticationMode: None` adds "unauthenticated users" | declared |
| `bindings` | Connection reference names, Dataverse bot id | |
| `extensions.copilot-studio` | Settings, conversation starters, AI settings, per-action tool, connector and credential mode, topics, the calls topics make and their credential mode, triggers | declared |

A `Maker` connection uses the credentials the agent's author configured, for every user. An `Invoker` connection uses each end user's own. The difference is often the most important governance fact about a Copilot Studio agent, so every action records its mode, and the agent's identity summarizes them.

## Writing

**Tested:** the golden tests validate each written file that has a `kind` against Microsoft's published Copilot Studio authoring schema, and check that every loss is explained. Settings and connection reference files have no published schema. The output hasn't yet been applied to a Copilot Studio environment.

The writer produces `agent.mcs.yml`, `settings.mcs.yml`, an action file per tool it can express, `connectionreferences.mcs.yml` for the connectors those actions use, and a knowledge file per website, SharePoint site and uploaded file.

- **Connection modes:** each action keeps its source connection mode. Without one, an agent that acts on behalf of users gets `Invoker`; otherwise it gets `Maker`.
- **Access:** without an access policy in the source, the agent is limited to users it is shared with, with integrated authentication. The report lists this as defaulted.
- **Identity:** an agent from another format keeps its ledger identity in a `# io.github.marshal-ledger:` comment at the top of `settings.mcs.yml`.

### Declared losses

| Field | Reason |
|---|---|
| `tools[]` that aren't connectors, MCP connectors, flows or agents | Copilot Studio needs a custom connector for them |
| `tools[].scope`, `reversible`, `output_schema` | Actions don't declare these |
| `knowledge[]` that isn't a website, SharePoint site or uploaded file | Not a Copilot Studio knowledge source |
| `permissions.grants`, `principal`, `identity`, `access` | Permissions come from connection modes and one agent-wide access policy |
| `model.parameters`, `model.resolved` | Not exposed by Copilot Studio |
| Topics, triggers, sub-agent content | Recorded by name; not re-created. A connector or flow a topic calls is written as an action. |
