# Agent Definition Language (ADL)

Format id: `adl`. Reads and writes [ADL](https://adl-spec.org) documents, one agent per YAML or JSON file, identified by an `adl_spec` key. The writer produces ADL 0.3 and validates against the published 0.3 schema in tests.

ADL is published under Apache 2.0 with an irrevocable patent non-assertion covenant that covers conforming implementations.

## Read mapping

| Ledger field | ADL source | Provenance |
|---|---|---|
| agent id | `id`; otherwise a slug of `name` | |
| `display_name`, `description` | `name`, `description` | declared |
| `model.requested`, `model.provider` | `model.name`, `model.provider` | declared |
| `model.parameters` | `model.temperature`, `model.max_tokens` | declared |
| `instructions` | `system_prompt`, or its `template` | declared |
| `tools[]` | `tools[]`: name, description, `parameters` → input schema, `returns` → output schema | declared |
| `tools[].scope` | `read_only: true` → read; `read_only: false` → write | declared / derived |
| `tools[].reversible` | `true` for read-only tools | derived |
| `tools[].requires_confirmation` | `requires_confirmation` | declared |
| `knowledge[]` | `resources[]`: name, description, type | declared |
| `permissions.identity`, `principal` | `cryptographic_identity.did` → the agent's own identity | derived / declared |
| `permissions.grants[]` | `permissions.filesystem.allowed_paths`, `permissions.network.allowed_hosts` | declared |
| `extensions.adl` | Everything else: version, lifecycle, provider, data classification, security, runtime, metadata, profiles, profile-specific sections, and the full `permissions` block | declared |

Tool refs are `tool/openapi.<openapi_ref>#<operation_id>` when a tool's annotations name an OpenAPI operation, so agents that call the same operation share the entity. Otherwise refs are scoped to the agent: `tool/adl.<agent>.<tool>`. Resources with a `uri` use it as their ref.

## Declared losses when writing ADL

| Field | Reason |
|---|---|
| `model.resolved` | ADL describes the requested model only |
| `model.parameters` other than temperature and max_tokens | Not in ADL's model block |
| `tools[].reversible` | No reversibility field; only read-only tools read back as reversible |
| `tools[].scope` of `execute` or `admin` | ADL can only say whether a tool is read-only |
| `knowledge[].scope` | Resources don't declare an access scope |
| `permissions.identity`, `principal` | ADL identifies an agent only by a DID |
| `permissions.grants[]` | Only network and filesystem boundaries are expressible |
| `permissions.access[]` | No list of who may use the agent |

## Values the writer fills in

ADL requires some fields that other formats may not have. The writer fills them in and the fidelity report lists each one as defaulted:

- `description`: the name.
- `version`: `0.0.0`.
- `data_classification`: `restricted`, the most protective level, so that it gets reviewed.
