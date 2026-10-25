# Databricks Asset Bundle reader

Format id: `databricks-asset-bundle`. Reads a `databricks.yml` (or `databricks.yaml`, `bundle.yml`, `bundle.yaml`) and every file matched by its `include` globs.

```console
$ marshal read path/to/bundle
$ marshal read path/to/bundle --target prod
$ marshal read path/to/bundle --hash-only --output json
```

## What counts as an agent

| Resource | Recorded as an agent when | Otherwise |
|---|---|---|
| `genie_spaces` | Always | |
| `apps` | The app declares a `serving_endpoint`, a `genie_space`, or a `uc_securable` of type `MODEL_SERVICE` | Skipped: "app declares no access to a model or an agent" |
| `model_serving_endpoints` | Never | Skipped: a bundle can't show whether an endpoint serves an agent |

Each record's `source.detected_by` names the rule that matched. Skipped items appear in the read result, and the CLI prints them to stderr.

## Identity

The agent id is `<bundle name>/<resource key>`, for example `genie_space_nyc_taxi/nyc_taxi_genie`. The resource key is stable across targets; the deployed name is not.

## Field mapping

### Genie spaces

| Ledger field | Source | Provenance |
|---|---|---|
| `display_name` | `title` | declared |
| `description` | `description` | declared |
| `instructions` | `instructions.text_instructions[].content` in the serialized space | declared |
| `knowledge[]` | `data_sources.tables[].identifier`, with scope `read` | declared ref, derived scope |
| `model.requested` | Platform-managed | unknown |
| `permissions.identity` | `on_behalf_of_user`: Genie queries run with the asking user's permissions | derived |
| `permissions.access[]` | `permissions` | declared |
| `extensions.databricks` | Sample questions, example SQL count, digest of the full serialized space | declared |
| `bindings` | `warehouse_id`, `parent_path` | |

The serialized space is read from `file_path` (resolved relative to the file that declares the resource), or from an inline `serialized_space` given as a JSON string or YAML mapping.

### Apps

| Ledger field | Source | Provenance |
|---|---|---|
| `display_name` | `name` | declared |
| `description` | `description` | declared |
| `model.requested`, `provider`, `instructions`, code tools | The app's agent code at `source_code_path`, read by the [Python reader](python.md) when it finds one entry-point agent; otherwise unknown | declared / derived / unknown |
| `permissions.identity` | `service_principal`, or `mixed` when `user_api_scopes` is set | derived |
| `permissions.grants[]` | Every entry in `resources`, with its `permission` | declared |
| `tools[]` | `genie_space`, `uc_securable` of type `FUNCTION` or `CONNECTION`, `job` | declared ref, derived scope |
| `knowledge[]` | `uc_securable` of type `TABLE` or `VOLUME` | declared ref, derived scope |
| `extensions.databricks` | `command`, `source_code_path`, `compute_size`, `user_api_scopes`, environment variable names and `value_from` references | declared |
| `bindings` | Deployed name when a target overrides it | |

A Genie space used as a tool gets scope `read` and `reversible: true`, since Genie runs only read-only SQL. A reference such as `${resources.genie_spaces.<key>.space_id}` links to that Genie space's agent record in the same bundle.

### Privilege to scope

| Scope | Privileges |
|---|---|
| `read` | `CAN_VIEW`, `CAN_READ`, `READ`, `READ_VOLUME`, `SELECT` |
| `execute` | `CAN_QUERY`, `CAN_RUN`, `CAN_USE`, `CAN_MANAGE_RUN`, `EXECUTE`, `USE_CONNECTION` |
| `write` | `CAN_EDIT`, `WRITE`, `WRITE_VOLUME`, `MODIFY`, `CAN_CONNECT_AND_CREATE` |
| `admin` | `CAN_MANAGE`, `MANAGE`, `IS_OWNER` |

`execute` means the agent can run something whose side effects the bundle doesn't declare, such as a UC function. Its `reversible` value stays `unknown`.

## Targets

Without `--target`, the reader records the base definition. With `--target`, it deep-merges that target's `resources` over the base, and records the target name, mode and workspace host as bindings.

When a target only renames resources or changes environment-bound values, the version is unchanged. When it changes what the agent can access, the version changes. Lists in target overrides replace the base list, and the reader warns when that happens.

Variable references (`${var.*}`, `${resources.*}`) are kept unresolved. An unresolved reference is the same in every environment, so it doesn't split versions.

## Redaction

The reader copies an allowlist of fields. Environment variable values are never copied, only their names and `value_from` references. Instructions and descriptions pass through credential redaction, and each redaction produces a warning.

## Known gaps

- Apps whose code isn't in the bundle directory, or whose code has several entry-point agents, keep model and instructions unknown.
- `model_serving_endpoints`, `mcp_services` and `vector_search_indexes` are not yet linked to the agents that use them.
- Variables are not resolved, including `lookup` variables.
