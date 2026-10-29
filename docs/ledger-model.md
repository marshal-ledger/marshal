# Ledger model v0

The ledger model is the hub every reader maps into and every writer maps out of. It is defined as Pydantic models in [`src/marshal_ledger/model.py`](../src/marshal_ledger/model.py). The JSON Schema in [`schema/marshal-v0.schema.json`](../schema/marshal-v0.schema.json) is generated from them.

## Records

| Kind | Meaning |
|---|---|
| `Agent` | The logical identity, such as `cfn/hr-resources/HRAgent` |
| `AgentVersion` | One content-addressed version of an agent, read from one source artifact |
| `Deployment` | A version placed in an environment, as a `proposed` or `shipped` event |
| `Entity` | A shared tool, knowledge source, model, store or other dependency that many agents can reference |

## Values and provenance

Every scored field is a `Value`:

```yaml
scope:
  value: write
  provenance: derived         # declared | attested | annotation | derived | unknown
  basis: 'widest action in the IAM role HRAgentActionGroupLambdaRole of executor Lambda BedrockHRFunction: s3:PutObject'
```

- `declared`: stated in the source artifact.
- `attested`: stated by a person in a hand-written definition ([Marshal file](formats/marshal-file.md)), not read from the platform. `basis` names the file and, where given, who attested it and when.
- `annotation`: from a hint attached to the source, such as MCP tool annotations.
- `derived`: computed by a documented rule from declared values. `basis` names the rule.
- `unknown`: not available. The ledger stores no value rather than guess.

## What a spec holds

| Field | Contents |
|---|---|
| `display_name`, `description` | Human-readable identity |
| `model` | `requested` model, `provider`, `resolved` model (where a gateway is connected), declared generation `parameters` |
| `instructions` | Hash of the instruction text, and the text unless hash-only mode is on |
| `tools[]` | Entity ref, name, description, input and output JSON Schemas, `scope`, `reversible`, `requires_confirmation` |
| `knowledge[]` | Entity ref, name, description, the source's own type, `scope` |
| `permissions` | `identity` kind, `principal`, `grants` held by the agent's identity, `access` rules for who may use the agent |
| `extensions` | Platform- or format-specific fields with no portable equivalent, kept intact per platform |

## Spec and bindings

`spec` is the environment-independent definition: name, description, model, instructions, tools, knowledge, permissions and platform extensions. `bindings` holds environment-bound values: target name, workspace host, warehouse IDs, deployed names.

Only `spec` is hashed, so the same definition in dev and prod is the same version.

## Version hash

`version` is `sha256:` over a canonical JSON form of `spec` (sorted keys, no whitespace, UTF-8). The hash excludes:

- `provenance` and `basis` metadata. Moving a file, or improving a reader's classification, doesn't create a new version.
- Instruction text. Instructions are represented by their own hash, so hash-only mode gives the same version as full mode.
- Absent values (`null`, empty lists, empty mappings). Adding an optional field to the model doesn't change existing versions.

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
| `observed`, `observed:prod` | The version most recently observed live |

`marshal diff AGENT` compares the latest version with the last shipped one. Diffs are computed on the ledger model, so they read the same on every platform. Changes are grouped by category, permissions and tools first.

## Channels, drift and freshness

Every event records its channel, which says how far to trust it:

| Channel | Source | Set by |
|---|---|---|
| `ci` | An authenticated pipeline | The ingest API, for `proposed` and `shipped` events |
| `scheduled` | A scheduled job that exported the platform's live definition | The ingest API, for `observed` events; `marshal collect --channel scheduled` locally |
| `manual` | A person ran a collection or imported a file | `marshal collect` (the default) |

Clients can't choose `ci`: the ingest API sets the channel from the authenticated request.

An `observed` event records what is live on the platform. When it differs from the version last shipped to that environment, the agent has **drifted**: something changed outside the pipeline, for example an edit in a platform's web UI. Drift clears when the agent is shipped again.

**Freshness** is the age of the latest `shipped` or `observed` event, judged by its channel:

| Channel | Stale after (default) |
|---|---|
| `manual` | 30 days |
| `scheduled` | 7 days (the scheduled export has stopped running) |
| `ci` | Never by age alone; drift detection covers changes made outside the pipeline |

`marshal ls` shows freshness per environment, and `marshal status AGENT` shows the shipped and observed versions. `marshal status --check` exits 1 on drift or staleness, for scheduled checks. Freshness is reported beside risk, not folded into it: how risky a change is and how far to trust the record are separate questions.

The store also indexes the tools, knowledge and grant targets of each version, which backs `marshal ls --tool` and `--knowledge` and dependency queries.

## Entity references

References use the form `<kind>/<platform>.<type>.<identifier>`, for example:

```text
tool/powerplatform.shared_office365users.Manager_V2
knowledge/databricks.uc.table.samples.nyctaxi.trips
model/databricks.serving_endpoint.databricks-claude-sonnet-5-5
```

An agent that another agent calls is referenced by its agent id: `agent/<agent id>`. Functions defined in code are scoped to their project (`tool/python.<project>/<module>:<function>`), so same-named functions in different projects are different entities.

`marshal deps PATTERN` lists the agents whose latest or shipped versions reference a matching tool, knowledge source or grant target, and where those versions are shipped. `marshal entities` lists shared entities with the number of agents that use them.

### Resolving refs across platforms

A ref names its platform, so an API called from two platforms has two refs. Marshal derives a *target* from what each ref states, and `marshal deps` also lists refs on any platform that share one with a match:

| Target | From | Match |
|---|---|---|
| `http:GET https://api.acme.example/invoices/status` | HTTP calls: Agent Spec remote tools and Copilot Studio HTTP request steps | same HTTP operation |
| `mcp:https://api.githubcopilot.com/mcp`, `mcp:npm:@notionhq/notion-mcp-server` | MCP servers, by URL or by the package an `npx` or `uvx` command runs | same MCP server |
| `openapi:<document>#<operationId>` | ADL tools annotated with an OpenAPI operation | same OpenAPI operation |
| `url:https://contoso.sharepoint.com/sites/hr` | Knowledge sources at a URL | same knowledge source |
| `lambda:arn:aws:lambda:...`, `lambda:import:<export>`, `lambda:function:<name>` | The Lambda function that runs a Bedrock action group: its ARN, the export it's imported from, or its stated name | same Lambda function, export or function name |
| `host:api.acme.example` | The host of any URL above, and network grants | same API host |

URLs are compared after lowercasing the host, dropping default ports, queries and trailing slashes, and writing every path parameter (`{id}`, `{{id}}`, `:id`) as `{}`. A same-host match is weaker: it says two agents reach the same system, not that they do the same thing, so it is listed after the others. Nothing is matched on a name alone, nothing is fetched, and a URL with a placeholder or loopback host has no target. `--exact` turns resolution off, and PATTERN can name a target directly, such as `marshal deps "host:api.acme.example"`.

In the demo, a proposed Copilot Studio topic calls the host that the public ADL examples' network boundaries allow:

<!-- example: deps-across-platforms -->
```
AGENT              VERSION  WHERE   RELATION  REF
test_allthethings  v2       latest  tool      tool/http.GET.https://api.acme.example/invoices/status

Reaching the same thing, on any platform:
AGENT                                          VERSION  WHERE          MATCH          VIA                    REF
https://agents.acme.example/invoice-processor  v1       prod (latest)  same API host  host:api.acme.example  other/network.api.acme.example
https://agents.acme.example/legacy-analyzer    v1       prod (latest)  same API host  host:api.acme.example  other/network.api.acme.example
```

A Lambda function is environment-bound, so it is recorded in each event's bindings (`bindings.executors`, by tool ref) rather than in the hashed spec, and matched across every environment a version was recorded in. A function name is unique only within an AWS account and region, which a template doesn't state, so that match says so. A function whose name CloudFormation generates belongs to one stack and isn't matched.

Some refs reach something their definition doesn't name, but the vendor documents it: Copilot Studio's Microsoft Learn Docs MCP connector is recorded as `tool/mcp.powerplatform.shared_microsoftlearndocsmcpserver`, with no URL, and Microsoft documents that it calls the server at `https://learn.microsoft.com/api/mcp`. A table of these [documented equivalents](../src/marshal_ledger/equivalents/README.md), each entry citing the vendor's pages, lets such a ref match agents that name the server. The match is reported as documented (for example `same MCP server (documented)`), and the JSON API's `basis` lists the sources. Entries are added only where vendor documentation states the link.

A person can also state a link no definition or document shows, in a [Marshal file's `links`](formats/marshal-file.md#links). Matches through it are labelled attested (`same HTTP operation (attested)`), and `basis` names the file, who attested it and when.

Tools whose definitions name only a platform object, such as a Power Platform connector operation, an Apex class or a Unity Catalog function, match exactly within their platform and aren't resolved further unless the table documents them.

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

## Risk ranking

Every change in a diff gets a severity, and reports list the highest first. An unknown scope ranks as a write, because the ledger doesn't assume an undeclared tool is safe.

| Severity | Changes |
|---|---|
| HIGH | Identity changed; grant added with write or admin scope; tool added with write, admin or unknown scope, or known to be irreversible; tool scope widened; tool became irreversible; tool no longer requires confirmation; model provider changed; instructions changed and a removed line held a guardrail word; knowledge added with write scope |
| MED | Principal changed; grant added with read or execute scope; access rule added; tool added with execute scope; tool reversibility became unknown; requested or resolved model changed within a provider, or model parameters changed; instructions changed; knowledge removed, added with unknown scope, or widened |
| LOW | Removals of tools, grants and access; read-only additions; metadata and other extension changes |

Instructions often carry an agent's guardrails, such as "never approve payment without AP approval," so a removed guardrail can widen what the agent does more than a new tool. When both versions' instruction text is stored, Marshal looks in the removed lines for words that usually state a limit: never, must not, do not, cannot, not allowed, forbidden, prohibited, approve or approval, confirm or confirmation, limit, maximum, at most, no more than, only if and unless. A word that also appears in an added line counts as reworded, not removed. The diff names the words it found. The public ADL invoice processor, with "and never approve payment without explicit AP approval" taken out of its instructions:

<!-- example: diff-guardrail -->
```
https://agents.acme.example/invoice-processor (adl)  v1 → v2

  HIGH  ~ instructions: +1 -1 lines; removed a line with 'never', 'approve', 'approval'

  Completeness 100%
```

This is a heuristic: it can't tell a guardrail from an ordinary sentence that happens to use one of these words, and in hash-only mode, with no text, a change to instructions ranks MED.

A different model from the same provider, such as a routine upgrade, ranks MED; a different provider ranks HIGH, because another party then sees the agent's data. An organization can list its approved models and providers in its [risk rules](guides/risk-rules.md); a change to any other is then raised.

Platform-specific fields that control identity or access rank by their key wherever they appear in an extension: connection and credential modes, authentication mode, access policy, agent user, user API scopes, authorizers and attached managed policies are HIGH; guardrails, security settings and permission blocks are MED.

The rules are in [`src/marshal_ledger/risk.py`](../src/marshal_ledger/risk.py). An organization's own rules, set on its ingest server, add to them; see [Your own risk rules](guides/risk-rules.md).
