---
title: "AI agent inventory from Python code: LangGraph, CrewAI, OpenAI Agents SDK and ADK"
description: >-
  How Marshal reads agents built with the OpenAI Agents SDK, LangChain and LangGraph, Google ADK and CrewAI by static analysis, without running the code.
---

# Agents in Python code

Format id: `python`. Reads agents built with the OpenAI Agents SDK, LangChain and LangGraph prebuilt agents, Google ADK, and CrewAI, by static analysis.

```console
$ marshal read path/to/agent/repo
$ marshal read path/to/agent/repo --format python
```

**The code is never imported or run.** Files are parsed with Python's `ast` module, and a small evaluator resolves only what can be known without running anything:

- literals, string concatenation, f-strings and `textwrap.dedent`
- names bound in the same function or module
- imports between the project's own modules, including relative imports and re-exports from `__init__.py`
- prompt files read with `Path(...).read_text()` or `open(...).read()`, when the file is inside the scanned directory

Everything else is recorded as unknown, with the reason: environment variables, function parameters, awaited values, conditional values.

## What counts as an agent

Each call to an agent constructor is one agent.

| Framework | Platform id | Constructors |
|---|---|---|
| OpenAI Agents SDK | `openai-agents` | `agents.Agent` (including `Agent[Context](...)`) |
| LangChain / LangGraph | `langchain` | `langchain.agents.create_agent`, `langgraph.prebuilt.create_react_agent` |
| Google ADK | `google-adk` | `google.adk.agents.Agent`, `LlmAgent` |
| CrewAI | `crewai` | `crewai.Agent`, including agents configured from `config/agents.yaml` |

### Agents without a framework

Code that calls a model API directly is also read. A model call that passes tools is an agent loop, so each such call site is recorded as an agent. Its record's `detected_by` says it was inferred, and `extensions.python.inferred` says why. A model call without tools is not an agent and is not recorded.

| API | Platform id | Calls |
|---|---|---|
| OpenAI (also Azure OpenAI, Databricks) | `openai-api` | `chat.completions.create`, `responses.create` |
| Anthropic (also on Bedrock and Vertex) | `anthropic-api` | `messages.create`, `messages.stream`, `beta.messages.create` |
| Google GenAI | `google-genai` | `models.generate_content` |
| LiteLLM | `litellm` | `completion`, `acompletion` |
| Bedrock Runtime | `bedrock-runtime` | `converse`, `converse_stream` on a `bedrock-runtime` client |

From the call the reader takes the model, the provider (from the client class), the system prompt (`system`, `instructions`, a `system` or `developer` message, `system_instruction`), generation parameters, and the tools declared there: function schemas, hosted tools, MCP servers. Tool refs are `tool/python.<project>/<module>:<tool name>`, the same as a function of that name in the module. `with_raw_response` wrappers are recognized. When the client can't be resolved statically, because it's chosen at run time, the agent is still recorded from the SDK the module imports, with the provider unknown. Turn inference off with `ReadOptions(infer_code_agents=False)`.

Parameters passed as `**kwargs` can't be seen statically, so such calls are not recorded. For code like that, commit an [ADL](adl.md) file next to it.

### Orchestration

Orchestration is reported as skipped, and its member agents are recorded: custom LangGraph `StateGraph`s, CrewAI `Crew`s, and ADK `SequentialAgent`, `ParallelAgent` and `LoopAgent`.

The agent id is `<directory name>/<agent name slug>`. The name comes from the constructor's `name` (or CrewAI's `role` or config key), else from the variable or function the agent is assigned to or returned from.

## Field mapping

| Ledger field | Source | Provenance |
|---|---|---|
| `display_name` | `name`; CrewAI `role` | declared |
| `description` | OpenAI `handoff_description`; ADK `description`; CrewAI `goal` | declared |
| `model.requested` | The model string or chat-model argument (`ChatDatabricks(endpoint=...)`, `LiteLlm(model=...)`, `LLM(model=...)`) | declared |
| `model.provider` | The chat-model class, a `provider:model` or `provider/model` prefix, a Gemini model name, or the OpenAI SDK's default client (`set_default_openai_client(AsyncDatabricksOpenAI())` → `databricks`) | derived |
| `model.parameters` | `ModelSettings`, chat-model arguments, ADK `GenerateContentConfig`, CrewAI `LLM` | declared |
| `instructions` | `instructions`, `instruction`, `system_prompt`, `prompt` | declared |
| | f-strings with unresolved parts keep a `{name}` placeholder for each part | derived |
| | CrewAI: `Role`, `Goal` and `Backstory`, as CrewAI composes them | derived |
| `tools[]` | Decorated tool functions: name, description from the docstring's first paragraph, input schema from the signature (context parameters excluded) | declared / derived |
| | Hosted and built-in tools (`WebSearchTool`, `google_search`, ...) with their scope | derived |
| | MCP servers (`MCPServerStreamableHttp`, `MCPToolset`, `DatabricksMCPServer`, ...) as `tool/mcp.<url or command>` | declared |
| | `UCFunctionToolkit(function_names=[...])` as `tool/databricks.uc.function.<name>`, the same refs as Databricks bundles | declared |
| | Other agents (handoffs, `as_tool`, `AgentTool`, ADK `sub_agents`) as `agent/<agent id>` | declared |
| `knowledge[]` | `FileSearchTool` vector stores; `VectorSearchRetrieverTool` indexes | declared |
| `permissions.identity` | Unknown: set by the runtime the agent is deployed to | unknown |
| `extensions.python` | Framework, constructor, module, and arguments with no ledger field | declared |

Function tools get the ref `tool/python.<project>/<module>:<function>`, where the project is the directory the module names are relative to. Agents in the same project that share a function share the tool entity, while same-named functions in different projects (for example two copies of a template) stay distinct.

## Linking to platform records

A Databricks App's `source_code_path` points at its agent code. The Databricks reader runs this reader on that directory. When it finds exactly one entry-point agent (one that no other agent calls), it adds that agent's model, instructions, tools and knowledge to the app's record, with `basis` pointing at the source line. The app's grants and identity still come from the bundle. Use `ReadOptions(link_code=False)` to read the bundle alone.

## Known gaps

- Custom LangGraph graphs are not analyzed.
- ADK Agent Config (`root_agent.yaml`) is not yet read.
- Tool lists built at run time, such as tools fetched from an MCP client with `await client.get_tools()`, are recorded under `tools_unresolved` in `extensions.python`.
