# Documented equivalents

Some refs reach an API or server that their definition doesn't name. A Copilot Studio agent's Microsoft Learn Docs MCP connector, for example, is recorded as `tool/mcp.powerplatform.shared_microsoftlearndocsmcpserver`, with no URL, but Microsoft documents that it calls the Microsoft Learn MCP Server at `https://learn.microsoft.com/api/mcp`.

These files list such links, one file per platform. Entity resolution uses them so the connector matches an ADK, Agent Spec or OASF agent that names that server's URL. A match through this table is reported as documented, with its sources.

Each entry has:

| Key | Meaning |
|---|---|
| `ref` | The exact entity ref |
| `target` | What it reaches, in the form `marshal deps` shows: `mcp:<url>`, `http:<METHOD> <url>`, `url:<url>`, `host:<host>` |
| `sources` | The vendor's own pages that, together, state the link |

Add an entry only when the vendor's documentation states it. A connector's name, a guess from how it behaves, or a third-party blog isn't enough. A server whose URL differs per tenant or organization, such as one under each Dataverse environment's own host, doesn't fit here, because the URL isn't fixed.

To add or correct an entry, edit the file, cite the pages in `sources`, and run `pytest`.
