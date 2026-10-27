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

**Tested:** every loss in converting every fixture is explained, the agent keeps its identity, and every written config validates against Google's published Agent Config schema. An optional test loads each one with ADK itself (`pip install google-adk mcp`, then `pytest tests/golden/test_adk_load.py`); with google-adk 2.13.0, every config loads apart from the agent's own Python functions, which ADK imports from the agent's project, and stdio MCP servers, which that ADK release refuses in configs, its own sample included.

ADK requires an instruction, so a source without one is written with an empty instruction and a warning. An MCP server is written only where the source says how to reach it: a URL, or an ADK config's own command. Environment values and headers for MCP servers aren't recorded, so they're left out, with a warning.

The writer produces `root_agent.yaml`: name (made a valid identifier), model, description, instruction, built-in, function and MCP tools, and generation parameters. The agent's ledger identity is kept in a `# io.github.marshal-ledger:` comment. Sub-agents are separate files and are converted on their own.

The conversion warns when the model isn't a Gemini model, since ADK Agent Config currently supports only Gemini. Function tool descriptions and schemas, scopes, knowledge sources and permissions are declared losses: in ADK they live in code, or don't exist.
