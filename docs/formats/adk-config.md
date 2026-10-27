# Google ADK Agent Config

Format id: `adk-config`. Reads and writes ADK Agent Config, the declarative YAML form of an ADK agent (`root_agent.yaml`). The reader starts from each `root_agent.yaml` and follows `config_path` references to sub-agents in other files.

| Ledger field | Source | Provenance |
|---|---|---|
| agent id | `<directory>/<name>` | |
| `display_name`, `description`, `instructions` | `name`, `description`, `instruction` | declared |
| `model.requested`, `provider` | `model`; Gemini names → `google` | declared / derived |
| `model.parameters` | `generate_content_config`: `temperature`, `top_p`, `max_output_tokens` | declared |
| `tools[]` | Built-ins (`google_search`, `url_context`, ...); Python functions by import path, as `tool/python.<project>/<module>:<function>` where the project is the folder containing the agent package, the same refs the [Python reader](python.md) gives when it reads the app from that folder; `LongRunningFunctionTool` and `FunctionTool` wrapping a function; `McpToolset` as `tool/mcp.<url or command>`; `AgentTool` and sub-agents as `agent/<id>` | declared / derived |
| `extensions.adk-config` | Tool arguments (environment values and headers removed), other `generate_content_config` settings, `output_key`, `include_contents` | declared |

Workflow agents (`SequentialAgent`, `ParallelAgent`, `LoopAgent`) are reported as skipped, and their sub-agents are recorded. MCP server environment values and headers are never copied.

## Writing

**Tested:** every loss in converting every fixture is explained, and the agent keeps its identity. The output hasn't yet been loaded by ADK.

The writer produces `root_agent.yaml`: name (made a valid identifier), model, description, instruction, built-in, function and MCP tools, and generation parameters. The agent's ledger identity is kept in a `# io.github.marshal-ledger:` comment. Sub-agents are separate files and are converted on their own.

The conversion warns when the model isn't a Gemini model, since ADK Agent Config currently supports only Gemini. Function tool descriptions and schemas, scopes, knowledge sources and permissions are declared losses: in ADK they live in code, or don't exist.
