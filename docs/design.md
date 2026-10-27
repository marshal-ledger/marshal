# Design

This page describes how Marshal works today: the problem it addresses, its architecture, its data model and security design, and what is next. Every example output on this page comes from running Marshal on the public agents that `marshal demo` records.

## The problem

Large organizations run agents on several platforms at once: Databricks, Copilot Studio, Bedrock, Vertex, Agentforce, and pro-code frameworks in their own repos. Each platform stores definitions in its own format, and new open standards (ADL, Open Agent Spec, OASF) add more formats rather than replacing the old ones. Nobody can answer basic questions across all of them:

- What agents exist, where do they run, and which team owns them?
- What changed between the version in production last month and today?
- Which agents use this tool, this knowledge source, or this write permission?
- Could this agent move to another platform, and what exactly would be lost?

Platform vendors have no reason to answer these questions across competitors' platforms, and the open standards ask authors to rewrite agents in a new format, which most teams won't do.

## The approach

Marshal never asks anyone to author in a new format or change how they deploy. It reads the artifacts teams already produce, builds a normalized, versioned record, and translates to whatever format someone needs next.

- **Any format in, any format out.** Closed platform formats and open standards are all adapters. No format is privileged, including Marshal's own.
- **Passive.** A CI step and optional scheduled exports collect definitions. Nobody maintains the ledger by hand.
- **Read-only.** Marshal never writes to repos or platforms, which keeps the security review simple.
- **Honest about fidelity.** Every record and every translation carries a completeness or fidelity score. The ledger never guesses: a value the source doesn't state is stored as `unknown`.
- **Self-hosted.** It runs inside the organization's network, on its own database.

## Architecture

```
COLLECTORS (passive)
  CI step (proposed + shipped events)  → deploy artifacts
  Scheduled exports (optional)        → UI-built agents, drift
  File import                          → exports, specs, manifests
          ↓
READERS (one per format)
  Databricks · Copilot Studio · Bedrock · Vertex · Agentforce
  LangGraph · CrewAI · OpenAI Agents SDK · ADL · Agent Spec · OASF
          ↓
LEDGER MODEL (hub) + VERSION STORE
  agents · versions · deployments
  tools · knowledge · permissions (shared entities)
  per-field provenance and quality scores
          ↓
WRITERS (one per format)          CAPABILITIES
  most readers' formats, plus  →  inventory · risk-ranked diff
  fidelity report                 portability scores · discovery
                                  audit trail · conversion
```

**Hub-and-spoke translation.** Every format gets one reader and, where it makes sense, one writer, both mapping to the ledger model. Adding a format costs two adapters, not one translator per pair of formats. The ledger model is a superset: it holds everything a standard holds, plus provenance, deployments, unknowns and platform extension blocks.

**Translation is easy to write and hard to make faithful.** Every adapter ships with:

- **Golden fixtures:** real public agent definitions for each format, pinned to a commit and fetched rather than vendored, never synthetic ones.
- **Readback fidelity:** a conversion writes the target, reads it back with that format's reader, and compares the two records field by field. See [Conversion and fidelity](conversion.md).
- **A declared loss table:** the fields a writer can't express, so every loss in a fidelity report has a stated reason.

## Formats and how each is verified

The formats table on the [home page](index.md#formats) lists what Marshal reads and writes. Readers are tested against real, public definitions, mostly from the platforms' own sample repositories, pinned to a commit. (Vertex AI Agent Engine has no public exports; its reader is tested against the documented API response shape.) Writers are tested at one of these levels:

| Level | What the tests check |
|---|---|
| Round trip and published schema | The output validates against the format's published schema, and reading it back gives the same record and version hash (ADL, Agent Spec) |
| Round trip | Reading the output back gives the same record and version hash (OASF, Agentforce Agent Script, Marshal file) |
| Published schema, partly | Each written file that has a published schema validates against it; every loss is explained (Copilot Studio, whose settings and connection reference files have no published schema) |
| Losses explained | Every loss in converting every fixture is explained, and the agent keeps its identity (CloudFormation, ADK Agent Config) |

No writer has yet been tested by importing its output into the platform itself. Until one has, read a written Copilot Studio, Agentforce or Bedrock definition as a well-formed starting point to review, not as a tested deployment.

## Data model

**Agent → Version → Deployment.** The full model is documented in [Ledger model v0](ledger-model.md).

- **Agent:** the logical identity, such as `eval_testAgent` or `cfn/hr-resources/HRAgent`.
- **Version:** content-addressed, a hash of the normalized definition (`spec`). Environment-bound values (endpoints, connection references, warehouse IDs) go in `bindings` and are excluded, so the same definition in dev and prod is the same version. Versions are selected as `v3`, `shipped:prod`, `proposed`, `latest` or a hash prefix such as `82c3255`.
- **Deployment:** version + environment + event + time + channel, with the commit and pipeline run where known. "Which version runs where" is a query.

**Shared entities.** Tools, knowledge sources and grant targets are records shared across agents, so the ledger can answer "who depends on this?" Entity refs have the form `<kind>/<platform>.<type>.<identifier>`, such as `tool/databricks.uc.function.main.billing.issue_refund`. Entities match on the exact ref, and the ref includes the platform, so today "who depends on this?" is answered within each platform. Recognizing that a Copilot Studio connector, an MCP tool and an OpenAPI operation call the same API is entity resolution, the next major milestone.

**Two events per deploy, and observations.**

- `proposed`: captured before deploy, from the pull request. Powers review.
- `shipped`: confirmed after the deploy succeeds. Diffs compare against the last shipped version, so failed deploys never show up as live.
- `observed`: the live definition, exported by a scheduled job or imported by hand. An observation that differs from what was shipped is drift: a change made outside the pipeline, such as an edit in a platform's web designer.

Every event records its channel (`ci`, `scheduled` or `manual`), and each agent's freshness per environment is the age of its last confirmation, judged by channel. Manual imports age out fastest. Freshness and drift are shown beside the risk-ranked diff, so a reviewer can tell a risky change from an untrustworthy record.

## Ingestion and authentication

Agent teams never hold database credentials. Readers run inside the team's own pipeline, and the resulting records go to the Marshal ingest API, which authenticates each pipeline by its identity rather than by a shared secret.

```
team pipeline                          Marshal (inside the org network)
  read artifacts → ledger records
  request OIDC token (aud: marshal)
  POST /v0/events  ──── HTTPS ────→   verify token signature, issuer, audience
                                       check claims against policy
                                       write to store
```

**Identity.** CI providers such as GitHub Actions and GitLab CI issue short-lived, signed OIDC tokens that name the repository, ref, workflow and deployment environment. Jenkins (with the OpenID Connect Provider plugin) and Azure DevOps issue tokens that name the job or pipeline. The Marshal step requests one at run time; nothing is provisioned per team and nothing needs rotating. Marshal accepts only tokens from configured issuers with its own audience, or with the fixed audience of a provider that doesn't let pipelines choose one (Azure DevOps).

**Authorization.**

- An agent id is bound to the repository that first records it. Later writes for that agent must come from the same repository; a Marshal administrator can transfer the binding.
- `shipped` events are accepted only from protected refs or deployment environments named in policy (for example `main`, or a GitHub environment named `prod`). This keeps a pull request from marking its own version as live.
- `proposed` events are accepted from any ref of the bound repository, including pull requests. They power review and are never treated as deployed.

**Separation.** Only the Marshal service holds database credentials. The web view and query API are read-only and authenticate users through the organization's SSO proxy.

**Network.** The ingest endpoint must be reachable from the runners. Self-hosted runners inside the network reach it directly. For hosted runners, the organization exposes the ingest endpoint alone (not the database or the web view) through its ingress, with OIDC as the only accepted credential.

**Local mode.** `marshal collect --db <file>` writes directly to a local SQLite store, for trials, tests and single-team use.

## Ledger record

Every field records where its value came from: `declared` (in the source artifact), `attested` (stated by a person in a hand-written definition), `annotation` (such as MCP tool hints), `derived` (computed by a rule, named in `basis`), or `unknown`. The public Bedrock HR assistant, as `marshal read` records it:

<!-- example: record-hr -->
```yaml
apiVersion: marshal/v0
kind: AgentVersion
agent: cfn/hr-resources/HRAgent
platform: bedrock
version: sha256:82c3255a1bc2ab6c4193fddb4c7f927bbd9289019d347c6227fb0bf1914faf95
source:
  format: cloudformation
  artifact: cfn/hr-resources.yml
  locator: Resources.HRAgent
  detected_by: resource type AWS::Bedrock::Agent
spec:
  model:
    requested:
      value: amazon.titan-text-premier-v1:0
      provenance: declared
      basis: Resources.HRAgent.Properties.FoundationModel
    provider:
      value: amazon
      provenance: derived
      basis: model id amazon.titan-text-premier-v1:0
    resolved:
      provenance: unknown
    parameters: {}
  instructions:
    hash: sha256:c10644a8665ef926393add9aae5ce4d5a071ebcf8097355cefa95d8f6a71d7fa
    text: You are an AI assistant specializing in HR-related inquiries…
    provenance: declared
    basis: Resources.HRAgent.Properties.Instruction
  tools:
  - ref: tool/bedrock.cfn.hr-resources.HRAgent.employee-timeoff-database
    name: employee-timeoff-database
    description: This action group contains tool to execute statements on the database to get or update
      time off balance.
    scope:
      value: write
      provenance: derived
      basis: 'widest action in the IAM role HRAgentActionGroupLambdaRole of executor Lambda BedrockHRFunction:
        s3:PutObject'
    reversible:
      provenance: unknown
    requires_confirmation:
      provenance: unknown
  permissions:
    identity:
      value: service_principal
      provenance: declared
      basis: 'Resources.HRAgent.Properties.AgentResourceRoleArn: IAM role AmazonBedrockExecutionRoleForAgentsHRAgent'
    principal:
      value: AmazonBedrockExecutionRoleForAgents_HRAgent
      provenance: declared
      basis: Resources.AmazonBedrockExecutionRoleForAgentsHRAgent.Properties.RoleName
    grants:
    - target: other/aws.*
      privilege: lambda:invokeFunction
      scope: write
      provenance: declared
      basis: cfn/hr-resources.yml#Resources.AmazonBedrockExecutionRoleForAgentsHRAgent
    - target: other/aws.*
      privilege: bedrock:InvokeModel
      scope: execute
      provenance: declared
      basis: cfn/hr-resources.yml#Resources.AmazonBedrockExecutionRoleForAgentsHRAgent
    access: []
quality:
  completeness: 0.73
# Abbreviated: three more tools, two more grants, the Bedrock extension block and the unknown fields.
```

The tool's scope is derived: the template doesn't state what the action group does, so Marshal takes the widest action in the IAM role of the Lambda function that runs it.

## Risk-ranked diffs

A diff between two versions is normalized (reordering tools or moving a file changes nothing) and each change is ranked by what it lets the agent do. A pull request comment for the demo's Copilot Studio agent, had its tool been switched to the maker's credentials in a pull request instead of in the web designer:

<!-- example: pr-comment-maker-credentials -->
```
**eval_testAgent** (copilot-studio) · v1 → v2 · 3 changes, 3 high risk

HIGH  ~ permissions.identity: mixed → shared_credential
HIGH  ~ extensions.copilot-studio.actions.SearchDocs.connection_mode: Invoker → Maker
HIGH  ~ extensions.copilot-studio.actions.SearchDocs.credentials: end user → maker
Completeness 38%
```

The rules are listed in [Risk ranking](ledger-model.md#risk-ranking). A tool whose scope is unknown ranks as a write.

## Quality and portability scores

**Completeness.** How much of the record is declared or derived versus `unknown`, weighted by importance (permissions and write tools count more than descriptions).

**Portability, per target platform.** Two parts, always shown separately: platform limitations from the public [capability matrix](portability.md#the-capability-matrix), and agent-specific issues found by converting the agent with the target's writer. The demo's proposed Databricks agent, assessed for Copilot Studio:

<!-- example: port-refund -->
```
agent_openai_agents_sdk/agent_openai_agents_sdk → Microsoft Copilot Studio

Portability: 58 / 100
  Platform limitations (copilot-studio):
    ~ model databricks-claude-sonnet-5-5 (databricks): other models; bring the model through Azure AI Foundry
    ~ identity service_principal: other identities; give each connection the maker's credentials or the end user's
    ~ tool issue_refund: other tools; expose the tool as an API and add it as a custom connector
    ~ tool get_current_time: other tools; expose the tool as an API and add it as a custom connector
  Agent-specific issues:
    ? tool get_current_time: scope unknown in the source
Fidelity: 5 lossless · 13 dropped · 2 defaulted
```

## What Marshal does today

| Capability | What it does |
|---|---|
| **Inventory** | Every agent, version and deployment, from every supported format |
| **Risk-ranked diff** | Normalized diff between versions, sorted by severity, posted as a pull request comment |
| **Drift and freshness** | What is live against what was shipped, and how recently each record was confirmed, by which channel |
| **Conversion** | Between supported formats, with a fidelity report computed by readback |
| **Portability scores** | Per target, split into platform limitations and agent-specific issues |
| **Discovery** | Search by tool, knowledge source, model or permission; "who depends on this?" within a platform |
| **Audit trail** | Every proposed, shipped and observed event: when, from which pipeline and commit |

It runs as a CLI with a local SQLite store, or as a service (ingest API and read-only web view) on Postgres, from a container image and Helm chart.

## Next

- **Entity resolution** across connector types and platforms for the same underlying API, from what the definitions state (such as a connector's or an OpenAPI schema's host and operation)
- **Policy checks:** rules such as "write-scope or irreversible tools need approval" or "models must be on an approved list," the same on every platform
- **Import-tested writers,** starting with Copilot Studio
- **Forking with lineage:** copy another team's agent into your platform's format and track the upstream
- **Agent bill of materials:** everything a version depended on
- **Resolved-model enrichment** where an AI gateway records which model served a request

## Non-goals

- Runtime evaluation or testing (that needs traces, which observability tools collect)
- Hosting or executing agents
- Writing to repos or platforms
- Scoring or monitoring individual people

## Security posture

- **Secret redaction in every reader.** Readers copy fields from allowlists, record environment variables by name only, and pass free text through redaction, so deploy artifacts' credentials never reach the store.
- **Hash-only mode:** store instruction hashes instead of prompt text. Diffs then report that instructions changed without showing them.
- **No shared secrets in pipelines.** CI authenticates to Marshal with short-lived OIDC tokens; agent teams never hold store credentials.
- **The CI path stands alone.** Scheduled exports need read access to each platform, so they're optional.
- **Self-hosted.** The service's only outbound requests fetch the configured OIDC issuers' discovery documents and signing keys.

See the [Security review](security-review.md) for the full threat model.

### Network posture

Marshal never starts a connection to an agent platform. Records reach it in two ways, both initiated by systems that already have access:

- **Files** that readers parse: repositories, exports, generated templates.
- **Pushes** to the ingest API, from deploy pipelines and from optional scheduled export jobs.

A scheduled export is a job the organization runs wherever it already has access to a platform, for example a nightly GitHub Actions or Azure DevOps workflow. It exports the live definition with that platform's own tools and pushes an `observed` event; ready-made workflows are in [`examples/scheduled/`](../examples/scheduled/). Where no scheduled export exists, Marshal still works: drift isn't detected, and records that only came from manual imports are reported as stale instead of current.

## Tech stack

- **Core, readers, writers:** Python and Pydantic. The JSON Schema for ledger records is generated from the models.
- **Store:** SQLite locally; Postgres for shared deployments.
- **CI:** a GitHub Action; GitLab CI, Jenkins and Azure DevOps through `marshal push` with OIDC tokens.
- **UI:** a read-only web view, server-rendered with no scripts.
- **Packaging:** a PyPI package, a signed container image and a Helm chart.

## Open questions

- How should completeness be weighted so the score reflects real governance risk?
- Should the capability matrix be versioned per platform release?
