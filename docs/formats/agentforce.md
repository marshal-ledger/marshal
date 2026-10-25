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

Tools of other kinds, other knowledge sources, the model, grants and access are declared losses. Legacy planner metadata (`GenAiPlannerBundle` XML) is not yet read.
