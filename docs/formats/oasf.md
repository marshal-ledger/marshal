# Open Agentic Schema Framework (OASF)

Format id: `oasf`. Reads and writes [AGNTCY OASF](https://docs.agntcy.org/oasf/) records (JSON or YAML) identified by `schema_version` and `name`. The writer produces OASF 1.1.0 for new records. When it re-writes an OASF record, it keeps that record's own schema version, because it copies the record's content as-is.

OASF records describe agents for discovery. They hold metadata, taxonomy skills and domains, locators and modules, but no instructions or permissions.

## Read mapping

| Ledger field | OASF source | Provenance |
|---|---|---|
| agent id | `name` | |
| `display_name`, `description` | `name`, `description` | declared |
| `model.requested`, `model.provider` | `core/language_model` module, when it lists exactly one model | declared |
| `tools[]` | `integration/mcp` module tools, as `tool/mcp.<server>.<tool>`; a server with no listed tools becomes one tool `tool/mcp.<server>` | declared |
| `knowledge[]` | `integration/mcp` module resources | declared |
| `bindings` | the language model's `api_base` | |
| `extensions.oasf` | version, authors, creation time, skills, domains, locators, annotations, and every module | declared |

MCP connection environment variables are kept by name and description only. Their `default_value` and any connection headers are never copied.

## Declared losses when writing OASF

| Field | Reason |
|---|---|
| `instructions` | OASF records describe capabilities, not instructions |
| `permissions.*` | No identity or permissions |
| `tools[]` that aren't MCP tools | OASF lists tools only as part of MCP server modules |
| `tools[].scope`, `reversible`, `requires_confirmation`, input and output schemas | MCP tools are listed by name and description only |
| `knowledge[]` | Data sources appear only as MCP server resources |
| `model.parameters`, `model.resolved` | Not in OASF model entries |

OASF requires at least one author and at least one skill from its taxonomy. The writer doesn't invent either. When the source has none, the conversion prints a warning, and the record won't validate until they're added.
