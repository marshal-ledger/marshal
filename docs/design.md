# Marshal — an agent ledger

**A vendor-neutral, open-source ledger for enterprise AI agents. Any format in, any format out, with versions, diffs, portability and audit across every platform.**

---

## The problem

Large organizations run agents on several platforms at once: Databricks, Copilot Studio, Bedrock, Vertex, Agentforce, and pro-code frameworks in their own repos. Each platform stores definitions in its own format, and new open standards (ADL, Open Agent Spec, OASF) add more formats rather than replacing the old ones. Nobody can answer basic questions across all of them:

- What agents exist, where do they run, and which team owns them?
- What changed between the version in production last month and today?
- Which agents use this tool, this knowledge source, or this write permission?
- Could this agent move to another platform, and what exactly would be lost?
- Has another team already built something like the agent I'm about to build?

Platform vendors have no reason to answer these questions across competitors' platforms, and the open standards ask authors to rewrite agents in a new format, which most teams won't do.

## The approach

Marshal never asks anyone to author in a new format or change how they deploy. It reads the artifacts teams already produce, builds a normalized, versioned record, and translates to whatever format someone needs next.

**Principles**

- **Any format in, any format out.** Closed platform formats and open standards are all just adapters. No format is privileged, including our own.
- **Passive.** A CI step and optional scheduled exports collect definitions automatically. Nobody maintains the ledger by hand.
- **Read-only by default.** It never writes to repos or platforms, which keeps the security review simple.
- **Honest about fidelity.** Every record and every translation carries a quality and fidelity score. The ledger never guesses, and an unknown value is stored as `unknown`.
- **Self-hosted.** It runs inside the organization's network, on their own database.

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
  any reader's format, plus  →    inventory · risk-ranked diff
  fidelity report                 portability scores · discovery
                                  audit trail · conversion
```

**Hub-and-spoke translation.** Every format gets one reader and one writer, both mapping to the ledger model. Adding a format costs two adapters, not one translator per pair of formats. The ledger model is a superset: it holds everything a standard holds, plus provenance, deployments, unknowns and platform extension blocks.

**Translation is easy to write and hard to make faithful.** Generating adapters is fast. Making them trustworthy is the real work. Every adapter ships with:

- **Round-trip tests:** read → write → read must produce the same ledger record, or the differences must be declared.
- **Golden fixtures:** real public agent definitions for each format, never synthetic ones.
- **A declared loss table:** which fields this writer can't express, so fidelity reports are computed, not guessed.

## Supported formats

Fidelity tiers keep the user experience smooth while coverage grows. Every major provider is supported at some tier from early on, and the score tells the user exactly what that tier means for their agent.

| Tier | Meaning |
|---|---|
| **Full** | Read and write, round-trip tested |
| **Partial** | Read and write, with declared gaps |
| **Inventory** | Read metadata only (name, owner, platform, version, deployments) |

| Format | Type | Tier (target) |
|---|---|---|
| Databricks Asset Bundles: Genie spaces | Closed, as-code | Full |
| Databricks Asset Bundles: agent apps | Closed, as-code | Partial (model and instructions from pro-code readers) |
| Copilot Studio agent definitions (YAML clone, solution export) | Closed | Partial → Full |
| Amazon Bedrock Agents (CloudFormation / CDK) | Closed, as-code | Partial → Full |
| Amazon Bedrock AgentCore Runtime | Closed, as-code | Inventory |
| Google Vertex AI Agent Engine | Closed | Inventory |
| Google ADK Agent Config, Conversational Agents export | Closed / open-source | Partial |
| Salesforce Agentforce (Agent Script, planner metadata) | Closed | Partial → Full |
| LangGraph, CrewAI, OpenAI Agents SDK, Google ADK | Pro-code | Partial via static extraction |
| ADL | Open standard | Full |
| Open Agent Spec | Open standard | Full for agents; flows recorded as their member agents |
| AGNTCY OASF | Open standard | Full (metadata) |
| Marshal file (hand-written definitions, for agents no reader covers) | Marshal-native | Full; values recorded as attested |

*Each adapter targets the platform's current documented export path. See [Platform exportability](platform-exportability.md) for what each closed platform exposes, [Conversion and fidelity](conversion.md) for how translation is measured, and [docs/formats](formats/) for each format's mapping and declared losses. ADL is published under Apache 2.0 with an irrevocable patent non-assertion covenant for conforming implementations.*

## Data model

**Agent → Version → Deployment**

- **Agent:** the logical identity, such as `invoice-agent`.
- **Version:** content-addressed, a hash of the normalized definition. Environment-bound fields (endpoints, connection references, environment variables) are excluded from the hash, so the same definition in dev and prod is literally the same version.
- **Deployment:** version + platform + environment + time + pipeline run. "Which version runs where" is a query.

**Shared entities.** Tools, knowledge sources and permissions are first-class records shared across agents, so the ledger can answer "who depends on this?" In the MVP, entities match only on exact identity. Recognizing that a Copilot connector, an MCP tool and an OpenAPI operation wrap the same underlying API is the hard entity-resolution problem and a planned milestone.

**Two events per deploy, and observations.**

- `proposed`: captured before deploy, from the PR. Powers review.
- `shipped`: confirmed after deploy succeeds. Diffs compare against the last shipped version, so failed deploys never show up as live.
- `observed`: the live definition, exported by a scheduled job or imported by hand. An observation that differs from what was shipped is drift: a change made outside the pipeline, such as an edit in a platform's web UI.

Every event records its channel (`ci`, `scheduled` or `manual`), and each agent's freshness per environment is the age of its last confirmation, judged by channel. Manual imports age out fastest. Freshness and drift are shown beside the risk-ranked diff, so a reviewer can tell a risky change from an untrustworthy record.

## Ingestion and authentication

Agent teams never hold database credentials. Readers run inside the team's own pipeline, and the resulting records go to a Marshal ingest API that authenticates each pipeline by its identity rather than by a shared secret.

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

**Separation.** Only the Marshal service holds database credentials. The web view and query API are read-only and authenticate users through the organization's SSO.

**Network.** The ingest endpoint must be reachable from the runners. Self-hosted runners inside the network reach it directly. For hosted runners, the organization exposes the ingest endpoint alone (not the database or the web view) through its ingress, with OIDC as the only accepted credential.

**Local mode.** `marshal collect --db <file>` writes directly to a local SQLite store, for trials, tests and single-team use.

## Ledger record

The full v0 model is documented in [Ledger model v0](ledger-model.md). Every field records where its value came from: `declared` (in the source artifact), `attested` (stated by a person in a hand-written definition), `annotation` (such as MCP tool hints), `derived` (computed by the ledger), or `unknown`. An abbreviated record:

```yaml
apiVersion: marshal/v0
kind: AgentVersion
agent: invoice-agent
version: sha256:7c41...
source:
  format: databricks-asset-bundle
  artifact: databricks.yml
  commit: 3f9c2e1
  pipeline_run: gh-actions/8812
spec:
  model:
    requested: claude-sonnet            # alias the agent asks for
    resolved: unknown                   # only filled where a gateway is connected
  instructions:
    hash: sha256:ab12...
    text: prompts/invoice.md            # omitted in hash-only mode
  tools:
    - ref: tool/sap.update_po
      scope:      { value: write,   provenance: declared }
      reversible: { value: unknown, provenance: unknown }
  knowledge:
    - ref: knowledge/uc.finance.vendor_faq_idx
  permissions:
    identity: service_principal
    principal: sp-invoice-agent
  extensions:
    databricks: { ... }                 # platform-specific fields kept intact
quality:
  completeness: 0.82
  unknown_fields: [tools[0].reversible, model.resolved]
```

## Quality and portability scores

Scores are how the ledger stays useful even where support is partial.

**Completeness.** How much of the record is declared or derived versus `unknown`, weighted by importance (permissions and write tools count more than descriptions).

**Portability, per target platform.** Two parts, always shown separately:

1. **Platform limitations** from the public capability matrix: what the target can't express at all.
   *"Copilot Studio has no equivalent for Unity Catalog vector index references."*
2. **Agent-specific issues:** features this particular agent uses that don't translate.
   *"Uses 2 Databricks-only functions; 1 tool has unknown write scope."*

**Translation fidelity.** Every conversion produces a report listing what carried over losslessly, what was approximated, and what was dropped.

```
$ marshal port invoice-agent --to copilot-studio

Portability: 71 / 100
  Platform limitations (copilot-studio):
    ✗ UC vector index → no native equivalent; needs a knowledge source rebuild
  Agent-specific issues:
    ~ instructions reference a Genie space by ID
    ? tool sap.update_po: write scope unknown in source
Fidelity: 14 fields lossless · 3 approximated · 2 dropped
```

### The capability matrix

A public, community-maintained data file per platform listing what it supports: tool types, knowledge source types, identity modes and model options, each as native, needs rework, or unsupported. It keeps portability scores honest and gives contributors an easy first PR. See [Portability](portability.md).

## Capabilities

### MVP

| Capability | What it does |
|---|---|
| **Inventory** | Every agent, version and deployment across platforms |
| **Risk-ranked diff** | Normalized diff between versions, sorted by severity: permission escalation, new write tools, model changes and removed knowledge first. Posted as a PR comment |
| **Conversion** | Any supported format to any other, with a fidelity report |
| **Portability scores** | Per target, split into platform limitations and agent-specific issues |
| **Discovery** | Search by tool, knowledge source, model or permission; "who depends on this?" |
| **Audit trail** | Who changed what, when, through which pipeline |

### Later

- **Entity resolution** across connector types for the same underlying API
- **Forking with lineage:** copy another team's agent into your platform's format and track the upstream
- **Agent bill of materials:** everything a version depended on, pinned where sources are connected
- **Rollback recipes:** the exact commit, artifact and command to revert to, run by the team's own pipeline
- **Policy checks:** rules like "write-scope or irreversible tools need approval," the same on every platform
- **Resolved-model enrichment** where an AI gateway is connected

### Non-goals

- Runtime evaluation or testing (needs traces most teams don't have)
- Hosting or executing agents
- Writing to repos or platforms by default
- Scoring or monitoring individual people

## Security posture

- **Secret redaction in every reader**, by design. Deploy artifacts often carry credentials and environment variables; they never reach the store.
- **Hash-only mode** per org: store instruction hashes instead of prompt text. Diffs then report "instructions changed" without showing content.
- **No shared secrets in pipelines.** CI authenticates to Marshal with short-lived OIDC tokens; agent teams never hold store credentials.
- **CI path stands alone.** Scheduled exports need read access to each platform, so they're optional, not required for core value.
- **Self-hosted** with no external calls.

### Network posture

Marshal never starts a connection to an agent platform. Records reach it in two ways, both initiated by systems that already have access:

- **Files** that readers parse: repositories, exports, generated templates.
- **Pushes** to the ingest API, from deploy pipelines and from optional scheduled export jobs.

A scheduled export is a job the organization runs wherever it already has access to a platform, for example a nightly GitHub Actions or Azure DevOps workflow. It exports the live definition with that platform's own tools and pushes an `observed` event; ready-made workflows are in [`examples/scheduled/`](../examples/scheduled/). Where no scheduled export exists, Marshal still works: drift isn't detected, and records that only came from manual imports are reported as stale instead of current.

## Demo

**GitHub Action, around the deploy step teams already have:**

```yaml
- uses: marshal-ledger/marshal/collect@v0
  with: { event: proposed, artifacts: ., target: prod, url: https://marshal.acme.example }
- run: databricks bundle deploy -t prod
- uses: marshal-ledger/marshal/collect@v0
  with: { event: shipped, artifacts: ., target: prod, url: https://marshal.acme.example }
```

See [GitHub Action](github-action.md) and [Ingest API](ingest-server.md).

**PR comment, ranked by risk:**

```
invoice-agent (Databricks)  v14 → v15

  HIGH  + tool: sap.update_po (write, reversibility unknown)
  HIGH  ~ permissions: principal gained 1 grant
  MED   - knowledge: vendor-faq-2023
  LOW   ~ instructions: 3 lines changed

  Completeness 82% · Portability: Bedrock 88 · Copilot Studio 71
```

**CLI:**

```
$ marshal ls --tool "sap.*"
$ marshal diff invoice-agent@v12 invoice-agent@v15
$ marshal convert agent.adl.yaml --to agent-spec
$ marshal port invoice-agent --to bedrock
```

## Roadmap

| Phase | Deliverable | Status |
|---|---|---|
| 1 | Exportability survey across Copilot Studio, Bedrock, Vertex and Agentforce. Ledger model v0. Databricks reader | Implemented |
| 2 | Version store with agent → version → deployment, proposed/shipped events, `marshal ls` and `marshal diff` | Implemented |
| 3 | Ingest API with OIDC authentication, GitHub Action, risk-ranked PR comments, secret redaction, hash-only mode | Implemented |
| 4 | Readers and writers for ADL, Open Agent Spec and OASF, round-trip tests, `marshal convert` with fidelity reports | Implemented |
| 5 | Pro-code readers by static extraction: OpenAI Agents SDK, LangChain/LangGraph, CrewAI, Google ADK, and direct model API calls; linked to Databricks Apps | Implemented |
| 6 | Copilot Studio, Bedrock Agents and Agentforce adapters; ADK Agent Config; inventory for AgentCore and Agent Engine; legacy Agentforce metadata | Implemented |
| 7 | Capability matrix v0, portability scores, `marshal port`; collection channels, drift and freshness | Implemented |
| 8 | Discovery and dependency queries, read-only web view | Implemented |
| 9 | Postgres store, container image and Helm chart; sign-in through the organization's SSO proxy | Implemented |
| 10 | Documentation, demo from public agents, launch | In progress |

Later milestones are listed under [Capabilities](#later): entity resolution, forking with lineage, an agent bill of materials, rollback recipes, policy checks, and resolved-model enrichment.

Pro-code readers come before the closed-platform adapters. Most agents on Databricks, AgentCore and Agent Engine are code agents, and their platform artifacts carry permissions and packaging but not behavior. The capability matrix follows the platform adapters, so portability scores cover every platform from the start.

## Target users and success metrics

**First users:** platform and AI enablement teams at organizations running agents on two or more platforms.

**Success looks like:**

- 3 platform teams running the Action in real pipelines
- Every major platform supported at Inventory tier or better
- Round-trip tests passing for all Full-tier formats
- Outside contributions to the capability matrix

## Tech stack

- **Core, readers, writers:** Python and Pydantic. The JSON Schema for ledger records is generated from the models and published with each release
- **Store:** SQLite or DuckDB locally; Postgres for shared deployments
- **CI:** GitHub Action first; GitLab CI, Jenkins and Azure DevOps through `marshal push` with OIDC tokens
- **UI:** a lightweight read-only web view
- **Packaging:** Docker image and Helm chart for self-hosting

## Open questions

- How should completeness be weighted so the score reflects real governance risk?
- Should the capability matrix be versioned per platform release?
- Which open standard gains the most adoption, and should the ledger model track it closely or stay deliberately independent?
