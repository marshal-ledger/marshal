# Marshal file (hand-written definitions)

Format id: `marshal-file`. A Marshal file is a YAML document, by convention named `*.marshal.yaml`, that describes one agent by hand. It's for agents that no reader covers: agents on an in-house platform, or in a vendor UI that has no export. A file is recognized by its `marshal: agent/v0` key.

```console
$ marshal init-agent refund-assistant.marshal.yaml --id payments/refund-assistant --platform orchestrator.internal
$ marshal read refund-assistant.marshal.yaml
$ marshal convert path/to/agent.adl.yaml --to marshal-file > agent.marshal.yaml   # start from an existing record
```

`init-agent` writes a commented template with every scored field set to `unknown`. A complete example is [examples/agents/refund-assistant.marshal.yaml](../../examples/agents/refund-assistant.marshal.yaml).

## Provenance: attested, not declared

The values in a Marshal file come from a person, not from the platform, so the reader records them as **`attested`**. A declared value was read from the platform's own artifact. An attested value is someone's claim about the platform, made by hand. Each value's `basis` names the file and field, and who attested it and when, if the file says:

```yaml
scope:
  value: write
  provenance: attested
  basis: refund-assistant.marshal.yaml#tools[1].scope (attested by payments-platform@example.com on 2026-10-20)
```

Both `attested_by` and `attested_at` are optional. They appear only in `basis` and `source.detected_by`, so re-attesting a file without changing a value doesn't create a new version. Like every provenance label, `attested` isn't part of the version hash: the same values in an ADL file and a Marshal file are one version.

A field the file leaves out is `unknown`, with the basis "not stated in the hand-written definition". A field set to the word `unknown` is also unknown, with a basis saying so. The reader never fills in a value. That includes reversibility for read-only tools, which it doesn't derive. Write `reversible: true` if you've checked it.

The web view shows an agent read from a Marshal file with a *hand-written* badge. Its attested values have a dashed underline, and hovering one shows its basis.

## Fields

Every ledger field has a place, with the same name:

| Key | Ledger field | Notes |
|---|---|---|
| `marshal` | | `agent/v0`. Required |
| `id` | agent id | Required. Stable across environments |
| `platform` | platform | Required. The platform's name, such as `orchestrator.internal` |
| `attested_by`, `attested_at` | `basis` of every value | Who checked the values, and when (`YYYY-MM-DD`) |
| `display_name`, `description` | same | |
| `model.requested`, `model.provider`, `model.resolved` | same | A bare string is `model.requested`. The provider is lowercased |
| `model.parameters` | same | Kept as written |
| `instructions` | `instructions` | The text, or `{hash: "sha256:…"}` when the text can't be shared |
| `tools[]` | `tools[]` | `ref`, `name`, `description`, `scope` (`read`, `write`, `execute`, `admin`), `reversible`, `requires_confirmation`, `input_schema`, `output_schema` |
| `knowledge[]` | `knowledge[]` | `ref`, `name`, `description`, `type`, `scope` |
| `permissions.identity` | same | `service_principal`, `on_behalf_of_user`, `shared_credential` or `mixed` |
| `permissions.principal` | same | The account the agent acts as |
| `permissions.grants[]` | same | `target`, `privilege`, `scope`, all three required. Each grant is attested |
| `permissions.access[]` | same | `principal`, `principal_type` (`user`, `group`, `service_principal`, `unknown`), `level` |
| `links[]` | `extensions.marshal-file.links` | `ref` and `same_as`: links between refs that no definition shows. See [Links](#links) |
| `bindings` | `bindings` | Environment-bound values. Not part of the version hash |
| `extensions` | `extensions` | Platform-specific fields, keyed by platform, kept as written |

Refs follow the ledger's `<kind>/<identifier>` convention. A ref without a `/` is qualified with the agent's platform. For example, a tool named `refund` becomes `tool/<platform>.refund`, a knowledge source becomes `knowledge/<platform>.<name>`, and a grant target becomes `other/<platform>.<target>`. A tool or knowledge source can be a bare ref string. Use the same ref in every file that names the same tool, so `marshal deps` finds every agent that uses it.

## Links

A definition often doesn't say what a tool reaches. A Python function that calls an HTTP API, or a Lambda function behind a Bedrock action group, is recorded by its own name, so it doesn't match the other agents that call the same API. A person who knows can say so:

```yaml
links:
  - ref: tool/python.billing/refunds:issue_refund      # any agent's ref, not only this file's
    same_as: http:POST https://api.acme.example/refunds
  - ref: tool/in-house.refunds
    same_as: tool/python.billing/refunds:issue_refund  # or another ref: the two are the same entity
```

`same_as` is a target, written as `marshal deps` shows it (`http:<METHOD> <url>`, `mcp:<url>`, `mcp:npm:<package>`, `url:<url>`, `host:<host>`), or a full ref. Targets are normalized like the ones Marshal derives. `marshal deps` then lists agents that reach the same thing through a link, labelled as attested, with the file and who attested it.

A link applies wherever the ref is used, so put it in the Marshal file of the agent it concerns, or in one your team keeps for the tools it owns. It counts while that file's agent has a current version, latest or shipped. Links change dependency results only; they don't change how risk is ranked.

## Validation

A hand-written file has typos, so the reader reports them as warnings rather than failing:

- A file without `id` or `platform` is skipped, with the reason.
- An invalid scope, identity or boolean is recorded as `unknown`, with a warning naming the field.
- A tool or knowledge source with neither `ref` nor `name` is skipped. So is a grant missing its target, privilege or scope, and an access rule missing its principal or level.
- A link without `ref` or `same_as`, with a ref that isn't a full ref, or with a `same_as` that is neither a target nor a full ref is skipped, with a warning.
- Unrecognized top-level keys, such as `tool:` for `tools:`, are kept under `extensions.marshal-file` with a warning, so nothing is lost silently.

## Credentials

A Marshal file is a source like any other, and the reader never copies credentials from it:

- Free text (descriptions, instructions, `attested_by`) passes through the redaction patterns.
- In `bindings`, `extensions`, tool schemas and model parameters, a value under a credential-like key (`password`, `secret`, `token`, `api_key`, `access_key`, `private_key`) becomes `<redacted>`. An `env` mapping keeps its variable names only. Other strings are redacted.

Each removal is reported as a warning.

## Writing Marshal files

`marshal convert --to marshal-file` writes any record as a Marshal file. Every ledger field has a place, so the round trip is lossless and keeps the version hash. Scored fields the record doesn't know are written as `unknown`, so the file shows a person what is left to check. The writer doesn't write `attested_by`: a conversion isn't an attestation. Once the file is read back, its values are attested by whoever the file names.

The only fields a conversion can lose are values the reader wouldn't copy back, such as a credential-like value in a platform extension. The fidelity report notes them.

## Gaps

- One agent per file. A YAML stream with several documents isn't read.
- Instructions must be inline. The file can't point to a separate prompt file.
- An attestation covers the whole file. A single value can't name a different person.
