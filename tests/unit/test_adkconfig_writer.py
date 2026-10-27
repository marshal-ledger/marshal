"""The ADK Agent Config writer produces configs ADK can load, without inventing connections."""

import textwrap

import yaml

from marshal_ledger.convert import convert
from marshal_ledger.hashing import sha256_text
from marshal_ledger.model import AgentSpec, Instructions, Source, ToolUse, Value
from marshal_ledger.readers.adkconfig import AdkConfigReader
from marshal_ledger.readers.base import build_record


def record(fmt, tools, text="Answer questions about the docs."):
    spec = AgentSpec(
        display_name=Value[str].declared("helper"),
        model={"requested": Value[str].declared("gemini-2.5-flash")},
        instructions=Instructions(hash=sha256_text(text), text=text) if text else Instructions(),
        tools=[ToolUse(ref=ref) for ref in tools],
    )
    return build_record(agent="helper", platform="p", source=Source(format=fmt, artifact="a"), spec=spec)


def toolsets(conversion):
    return [t["args"] for t in conversion.document.get("tools", []) if t["name"] == "McpToolset"]


def test_an_mcp_server_without_a_connection_is_not_written_as_a_command():
    # A Copilot Studio MCP connector names the connector, not a program to run.
    conversion = convert(record("copilot-studio", ["tool/mcp.powerplatform.shared_custommcp"]), "adk-config")
    assert toolsets(conversion) == []
    assert any("shared_custommcp" in w for w in conversion.warnings)
    notes = {f.path: f.note for f in conversion.report.lossy}
    assert notes["tools[tool/mcp.powerplatform.shared_custommcp]"] == "the source names this MCP server but not its URL or command"


def test_mcp_servers_with_a_url_or_an_adk_command_are_written():
    url = convert(record("oasf", ["tool/mcp.https://mcp.example.com/mcp"]), "adk-config")
    assert toolsets(url) == [{"streamable_http_connection_params": {"url": "https://mcp.example.com/mcp"}}]
    stdio = convert(record("adk-config", ["tool/mcp.npx -y some-server"]), "adk-config")
    assert toolsets(stdio) == [{"stdio_connection_params": {"server_params": {"command": "npx", "args": ["-y", "some-server"]}}}]


def test_redacted_environment_is_left_out_and_each_server_is_kept(tmp_path):
    (tmp_path / "root_agent.yaml").write_text(textwrap.dedent("""
        name: notion_agent
        model: gemini-2.5-flash
        instruction: Use the tools to search Notion and the docs.
        tools:
          - name: McpToolset
            args:
              stdio_connection_params:
                server_params:
                  command: npx
                  args: ["-y", "@notionhq/notion-mcp-server"]
                  env: {OPENAPI_MCP_HEADERS: '{"Authorization": "Bearer abc"}'}
          - name: McpToolset
            args:
              streamable_http_connection_params: {url: "https://docs.example.com/mcp"}
    """))
    [source] = AdkConfigReader().read(tmp_path).records
    conversion = convert(source, "adk-config")
    written = toolsets(conversion)
    assert written == [
        {"stdio_connection_params": {"server_params": {"command": "npx", "args": ["-y", "@notionhq/notion-mcp-server"]}}},
        {"streamable_http_connection_params": {"url": "https://docs.example.com/mcp"}},
    ]
    assert "Bearer" not in conversion.text and "<redacted>" not in conversion.text
    assert any("environment values" in w for w in conversion.warnings)


def test_a_missing_instruction_is_written_empty_with_a_warning():
    conversion = convert(record("oasf", [], text=None), "adk-config")
    assert yaml.safe_load(conversion.text)["instruction"] == ""
    assert not conversion.read_back.spec.instructions.hash  # reads back as no instructions, not as new ones
    assert any("requires an instruction" in w for w in conversion.warnings)
