# Ledger model v0

The ledger model is the hub every reader maps into and every writer maps out of. It is defined as Pydantic models in [`src/marshal_ledger/model.py`](../src/marshal_ledger/model.py). The JSON Schema in [`schema/marshal-v0.schema.json`](../schema/marshal-v0.schema.json) is generated from them.

## Records

| Kind | Meaning |
|---|---|
| `Agent` | The logical identity, such as `finance/invoice_agent` |
| `AgentVersion` | One content-addressed version of an agent, read from one source artifact |
| `Deployment` | A version placed in an environment, as a `proposed` or `shipped` event |
| `Entity` | A shared tool, knowledge source, model, store or other dependency that many agents can reference |

## Values and provenance

Every scored field is a `Value`:

```yaml
scope:
  value: write
  provenance: declared        # declared | annotation | derived | unknown
  basis: resources.apps.invoice_agent.resources[2]
```

- `declared`: stated in the source artifact.
- `annotation`: from a hint attached to the source, such as MCP tool annotations.
- `derived`: computed by a documented rule from declared values. `basis` names the rule.
- `unknown`: not available. The ledger stores no value rather than guess.

## Spec and bindings

`spec` is the environment-independent definition: name, description, model, instructions, tools, knowledge, permissions and platform extensions. `bindings` holds environment-bound values: target name, workspace host, warehouse IDs, deployed names.

Only `spec` is hashed, so the same definition in dev and prod is the same version.

## Version hash

`version` is `sha256:` over a canonical JSON form of `spec` (sorted keys, no whitespace, UTF-8). The hash excludes:

- `provenance` and `basis` metadata. Moving a file, or improving a reader's classification, doesn't create a new version.
- Instruction text. Instructions are represented by their own hash, so hash-only mode gives the same version as full mode.

Tools, knowledge, grants and access rules are sorted before hashing, so reordering them doesn't create a new version. Lists inside `extensions` keep their order.

## Version store

The store keeps each (agent, version hash) once and numbers versions per agent in the order it first saw them: `v1`, `v2`, and so on. Sequence numbers are local to a store; the hash is the portable identity.

Every collection adds an event, `proposed` or `shipped`, for an environment. Bindings, commit and pipeline run are stored on the event, not on the version. Which version runs where is the most recent `shipped` event per environment, so re-shipping an older version (a rollback) is recorded as such.

Commands and the store API select versions with:

| Selector | Meaning |
|---|---|
| `v3` | Sequence number |
| `sha256:7c41a9e` or `7c41a9e` | Version hash or a unique prefix of at least 7 hex digits |
| `latest` | The version in the agent's most recent event |
| `shipped`, `shipped:prod` | The version most recently shipped, anywhere or to one environment |
| `proposed`, `proposed:prod` | The version most recently proposed |

`marshal diff AGENT` compares the latest version with the last shipped one. Diffs are computed on the ledger model, so they read the same on every platform. Changes are grouped by category, permissions and tools first.

The store also indexes the tools, knowledge and grant targets of each version, which backs `marshal ls --tool` and `--knowledge` and, later, dependency queries.

## Entity references

References use the form `<kind>/<platform>.<type>.<identifier>`, for example:

```text
tool/databricks.uc.function.main.support.lookup_order
knowledge/databricks.uc.table.samples.nyctaxi.trips
model/databricks.serving_endpoint.databricks-claude-sonnet-5-5
```

An agent that another agent calls is referenced by its agent id: `agent/<agent id>`. In v0, two references are the same entity only when they match exactly. Resolving different connectors for one underlying API is a later milestone.

## Completeness

`quality.completeness` is the weighted share of scored fields that are known. `quality.unknown_fields` lists the rest. Weights favor what matters for governance:

| Field | Weight |
|---|---|
| `permissions.identity` | 3 |
| `tools[].scope` | 3 |
| `permissions.principal` | 2 |
| `tools[].reversible` | 2 |
| `model.requested` | 2 |
| `instructions` | 2 |
| `knowledge[].scope` | 1 |
| `description`, `display_name` | 0.5 each |
