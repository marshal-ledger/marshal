# Microsoft Copilot Studio

Format id: `copilot-studio`. Reads and writes the agent workspaces that the Copilot Studio extension for Visual Studio Code clones and applies: a folder of `.mcs.yml` files. Tests validate written files against the Copilot Studio authoring schema, by each file's `kind`.

```console
$ marshal read path/to/cloned-agent
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

Display names come from the files' `# Name:` comment headers where present. A file wrapped in a Markdown code fence is still read.

## Read mapping

| Ledger field | Source | Provenance |
|---|---|---|
| agent id | `settings.mcs.yml` `schemaName` (sub-agents: `<schema name>/<folder>`) | |
| `instructions` | `agent.mcs.yml` `instructions` (sub-agents: `settings.instructions`) | declared |
| `model.requested`, `provider` | `aISettings.model.modelNameHint` and `provider`; GPT hints → `azure-openai`, Claude hints → `anthropic` | declared / derived |
| `tools[]` | Connector actions as `tool/powerplatform.<connector>.<operation>`; MCP connectors as `tool/mcp.powerplatform.<connector>`; flows as `tool/powerplatform.flow.<id>`; connected agents and sub-agents as `agent/<id>`; `webBrowsing` and `codeInterpreter` capabilities; HTTP requests in topics | declared / derived |
| `tools[].input_schema` | Inputs the model fills in (`AutomaticTaskInput`) | declared |
| `tools[].requires_confirmation` | An action's `confirmation` prompt | declared |
| `knowledge[]` | Sites as `knowledge/<url>`, so a SharePoint site shared with another agent is the same entity; uploaded files; scope read | declared / derived |
| `permissions.identity` | Derived from every action's and trigger's connection mode: all `Invoker` → `on_behalf_of_user`; all `Maker` (the schema default) → `shared_credential`; both → `mixed` | derived |
| `permissions.access[]` | `accessControlPolicy`; `authenticationMode: None` adds "unauthenticated users" | declared |
| `bindings` | Connection reference names, Dataverse bot id | |
| `extensions.copilot-studio` | Settings, conversation starters, AI settings, per-action connector and credential mode, topics, triggers | declared |

A `Maker` connection uses the credentials the agent's author configured, for every user. An `Invoker` connection uses each end user's own. The difference is often the most important governance fact about a Copilot Studio agent, so every action records its mode, and the agent's identity summarizes them.

## Writing

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
| Topics, triggers, sub-agent content | Recorded by name; not re-created |
