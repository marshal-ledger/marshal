# Salesforce Agentforce (Agent Script)

Format id: `agentforce`. Reads and writes Agent Script `.agent` files, the source format of the Agentforce Builder (`aiAuthoringBundles/<Name>/<Name>.agent`, retrieved with `sf project retrieve start --metadata AiAuthoringBundle:<Name>`).

## Read mapping

| Ledger field | Source | Provenance |
|---|---|---|
| agent id | `config.developer_name` | |
| `display_name`, `description` | `config.agent_label`, `config.description` | declared |
| `instructions` | `system.instructions` | declared |
| `model` | Unknown: chosen by the Agentforce reasoning engine | unknown |
| `permissions.identity`, `principal` | `access.default_agent_user` → the agent's own user; employee agents without one run as the signed-in user | declared / derived |
| `tools[]` | Every action definition, by target: `tool/salesforce.flow.<name>`, `.apex.`, `.prompt.`, `.standard.`; inputs and outputs as JSON Schema; `require_user_confirmation`. `@utils.escalate` → `tool/salesforce.escalate` | declared |
| `knowledge[]` | `knowledge.rag_feature_config_id` → `knowledge/salesforce.data_library.<id>` (placeholders are skipped) | declared |
| `extensions.agentforce` | Config, messages, variables, language, knowledge settings, and each subagent's block verbatim | declared |

Subagent blocks, with their routing, procedural instructions and action wiring, are kept verbatim, so they are part of the version and survive a round trip unchanged.

## Writing

A native agent's blocks are written back as read. For agents converted from other formats, the writer:

- generates one entry subagent;
- writes Flow, Apex, prompt template and standard actions as action definitions;
- writes escalation as `@utils.escalate`;
- fills required messages and a default locale, each reported as defaulted.

Tools of other kinds, other knowledge sources, the model, grants and access are declared losses.

## Legacy Agent Builder metadata

Format id: `salesforce-metadata` (read only). Agents built in the legacy Agent Builder exist only as metadata, retrieved with `sf project retrieve start --metadata Agent:<Name>`. The reader reads an SFDX source folder:

| Ledger field | Source | Provenance |
|---|---|---|
| agent id | The planner's developer name: the same id the agent keeps in Agent Script | |
| `display_name`, `description` | Bot label; planner description | declared |
| `permissions.identity`, `principal` | The bot's agent type: service agents run as their `botUser`, employee agents as the signed-in user. A `%%placeholder%%` agent user is recorded as unknown | declared / derived |
| `instructions` | Composed from each topic's classification, scope and instructions; legacy agents have no single system prompt | derived |
| `tools[]` | Every action of the planner and its topics, inline or referenced: `GenAiFunction`s by target (`tool/salesforce.flow.<name>`, `.apex.`, `.prompt.`, `.standard.`) with input and output schemas and `isConfirmationRequired`; standard actions without local metadata by API name (`tool/salesforce.function.<name>`) | declared |
| `extensions.salesforce-metadata` | Agent type, topics (description, scope, instructions, actions, escalation), default outbound flow | declared |

Topics and actions are looked up in the planner's own package first, then in other packages under the folder being read.
