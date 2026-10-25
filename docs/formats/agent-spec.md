# Open Agent Specification (Agent Spec)

Format id: `agent-spec`. Reads and writes [Agent Spec](https://github.com/oracle/agent-spec) documents (YAML or JSON) identified by `component_type`. The writer produces Agent Spec 26.3.1.

Tests validate written documents against the published 26.3.1 JSON Schema. That schema declares `agentspec_version` beside a closed `Agent` definition, so no versioned document can validate as a whole. The tests therefore check the version against its enum and the rest of the document against `Agent`.

## Reading

The reader resolves `$component_ref` references, then records every `Agent` component it finds, including agents inside flows, swarms and manager-worker groups. `Flow`, `Swarm` and `ManagerWorkers` components are reported as skipped. Their orchestration isn't represented in the ledger model yet. `A2AAgent`, `OciAgent` and `RemoteAgent` are skipped too, since they refer to agents defined elsewhere.

| Ledger field | Agent Spec source | Provenance |
|---|---|---|
| agent id | `<file stem>/<agent name slug>` | |
| `display_name`, `description` | `name`, `description` | declared |
| `model.requested` | `llm_config.model_id` | declared |
| `model.provider` | `llm_config.provider` or `api_provider`; otherwise the config type (`OpenAiConfig` → `openai`, `VllmConfig` → `vllm`, ...) | declared / derived |
| `model.parameters` | `llm_config.default_generation_parameters` | declared |
| `instructions` | `system_prompt` | declared |
| `tools[]` | `tools[]` and `toolboxes[]`: name, description, `inputs`/`outputs` as JSON Schema | declared |
| `tools[].scope` | Remote tools: `GET`/`HEAD`/`OPTIONS` → read, other methods → write | derived |
| `tools[].requires_confirmation` | `requires_confirmation`, which defaults to `false` | declared / derived |
| `bindings` | `llm_config.url`, `compartment_id` | |
| `extensions.agent-spec` | LLM config type and name, agent inputs and outputs, tool and toolbox details, `human_in_the_loop`, metadata | declared |

Tool refs: remote tools are `tool/http.<METHOD>.<url>`; MCP tools are `tool/mcp.<server URL or command>.<tool>`; built-in tools are `tool/agentspec.builtin.<type>`; server and client tools are scoped to the agent.

**Credentials are never copied.** API keys, sensitive headers, headers, TLS key and certificate files, OCI client configs, auth configs and transport environment variables are left out. The reader warns when an LLM config held credentials.

## Declared losses when writing Agent Spec

| Field | Reason |
|---|---|
| `knowledge[]` | No knowledge-source component; retrieval is modeled as a tool |
| `permissions.*` | Agent Spec doesn't describe identity or permissions |
| `tools[].scope` | Only a remote tool's HTTP method states its effect |
| `tools[].reversible` | No reversibility field |
| `model.resolved` | Agent Spec describes the configured model only |

Optional tool parameters without a default read back as required, because Agent Spec marks a parameter optional only by giving it a default. A bare return schema is written as one output named `result`. When an `OllamaConfig` or `VllmConfig` endpoint URL isn't in the record, the writer uses a generic `LlmConfig`.
