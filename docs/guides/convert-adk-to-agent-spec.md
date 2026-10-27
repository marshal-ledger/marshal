# Convert a Google ADK agent to Open Agent Spec

`marshal convert` reads an agent in one format and writes it in another, then reads the result back and reports exactly what survived. Nothing is dropped silently: each field is lossless, dropped with a reason, or filled with a default the target requires.

## Convert

Take Google's ADK sample that gives an agent the Notion MCP server:

```console
$ pip install marshal-ledger
$ marshal convert samples/mcp/tool_mcp_stdio_notion_config --to agent-spec --out converted/
wrote converted/notion_agent.agentspec.yaml
tool_mcp_stdio_notion_config/notion_agent → agent-spec
  Fidelity: 8 lossless · 1 dropped · 3 defaulted
    - extensions.adk-config.* (1 field; adk-config-specific; no Agent Spec equivalent)
    + extensions.agent-spec.llm_config (Agent Spec requires an LLM config; one was built from the model fields)
    + tools[tool/mcp.npx -y @notionhq/notion-mcp-server].requires_confirmation (Agent Spec defaults requires_confirmation to false)
    + extensions.agent-spec.tools (MCP tool transports were built from the tool refs)
```

The written file is an Agent Spec `Agent` with an `LlmConfig` for `gemini-2.5-flash` and an `MCPTool` with a stdio transport. The agent's Marshal id travels in `metadata`, so converting it back finds the same agent.

The sample passes a Notion token to the MCP server in its environment. Marshal never copies environment values, so the token is in neither the record nor the converted file.

## Other targets

The same command writes ADL, OASF, Copilot Studio, Agentforce Agent Script, Bedrock Agents (CloudFormation), ADK Agent Config and Marshal files:

```console
$ marshal convert agent.adl.yaml --to copilot-studio --out converted/
$ marshal convert path/to/bundle --to cloudformation --report fidelity.json
```

Each writer lists the fields its format can't hold. See [Conversion and fidelity](../conversion.md).

## Before you move an agent

A conversion shows what one agent loses. A portability score adds what the target platform can't do at all, from a community-maintained [capability matrix](../portability.md):

```console
$ marshal port samples/mcp/tool_mcp_stdio_notion_config --to all
tool_mcp_stdio_notion_config/notion_agent  Portability: Salesforce Agentforce 57 · Amazon Bedrock Agents 50 · Microsoft Copilot Studio 64 · Databricks (Apps and Genie) 100 · Google ADK on Vertex AI Agent Engine 100
```
