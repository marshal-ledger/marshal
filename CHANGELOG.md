# Changelog

Notable changes to Marshal, newest first. Versions follow [Semantic Versioning](https://semver.org). Before 1.0, a minor release may change the ledger model, the ingest API or the command line; each such change is listed here.

## 0.5.0 (2026-10-27)

- Dependencies across platforms: `marshal deps`, the web view and `/v0/deps` also list refs on any platform that reach the same HTTP operation, MCP server, OpenAPI operation, knowledge URL or API host, each with how it matched (`match`, `via`). Matches come only from what the definitions state; nothing is fetched or matched on a name. `--exact` (or `resolve=false`) turns it off, and a pattern can name a target, such as `host:api.acme.example`.
- Bedrock action groups resolve to the Lambda function that runs them, so agents in different stacks that share a function match: by function ARN, by the export a stack imports it from, or by its stated name. The function is recorded in the event's bindings (`executors`), so versions don't change.
- Documented equivalents: a table of links vendors document but definitions don't state, each citing its sources (`equivalents/*.yaml`). It starts with Copilot Studio's Microsoft Learn Docs MCP connector and the server it calls, so that connector matches agents that name the server's URL. Such matches are reported as documented, and `/v0/deps` gains `basis`.
- Marshal files gain `links`: a person states that a ref reaches a target (`http:POST https://…`, `mcp:…`, `host:…`) or is the same entity as another ref, for tools whose definitions don't show it, such as a Python function that calls an API. Dependency matches through a link are labelled attested, with who attested it.
- `marshal demo` adds a proposed Copilot Studio topic that calls the API host the public ADL examples may reach, so the dependencies page shows a match across platforms.
- The CloudFormation writer sets `AutoPrepare` on the agent, as the AWS samples do, so a deployed agent is ready to test.
- Every YAML writer writes multi-line instructions as a literal block, as the Copilot Studio and Marshal file writers already did.

## 0.4.0 (2026-10-27)

- Risk ranking: a changed model ranks MED, and HIGH only when its provider changes, so routine upgrades no longer drown out escalations. Changed instructions rank MED instead of LOW, and HIGH when a removed line holds a word that usually states a limit (such as "never", "must not" or "approval"); the diff names the words. Diff changes carry a new optional `guardrails` field.
- The Copilot Studio writer writes multi-line instructions as a YAML block that reads as written, and its fidelity report explains a connection mode set from the agent's identity.
- The CloudFormation writer passes cfn-lint, which the golden tests now run on every written template: function parameter names are made valid for Bedrock, an imported value is written as `Fn::ImportValue`, and a reference to a resource in the source stack, such as a guardrail, becomes a template parameter instead of a literal `${...}` string.
- The ADK Agent Config writer's output validates against Google's published schema, which the golden tests now check, and loads in ADK: a missing instruction is written empty with a warning, an MCP server is written only where the source gives its URL or an ADK command (a Copilot Studio connector was written as a command to run), redacted MCP environment values and headers are left out instead of written as a string, and an agent with several MCP servers keeps each one.
- A conversion's fidelity report lists defaulted fields in the same order on every run.
- Example outputs in the documentation are generated from the demo and the public fixtures by `scripts/doc_examples.py`, and a test fails when one is out of date. The design page now describes Marshal as it is, with real output in place of illustrative examples.
- The formats tables say how each writer is tested, and that no writer's output has yet been imported into its platform.
- The documentation says plainly that dependency queries match entities within a platform; matching across platforms is the next milestone.

## 0.3.0 (2026-10-27)

- `marshal web --export DIR` writes the web view as static files, for sharing a ledger without running a server.
- `marshal demo` reads its proposed change from a folder with the agent's own name, so the agent's Python tool keeps its ref and the diff no longer shows it removed and re-added. The proposal now ranks MED: a refund function and the grant to run it.
- Documentation site at [marshal-ledger.github.io/marshal](https://marshal-ledger.github.io/marshal/), with a live demo, a web view walkthrough, an architecture diagram and screenshots in the README.
- Guides for inventorying Copilot Studio agents, reviewing agent changes in pull requests, tracking agents in code, detecting drift, converting an ADK agent to Agent Spec and preparing audit evidence, and a page on how Marshal compares with related tools.
- The source archive leaves out the site's images.

## 0.2.0 (2026-10-26)

- Postgres stores replace a lost connection and retry the read that found it; a write is never replayed, and the ingest API and web view answer `503` while the database is unreachable. A new `store_auth` setting (`--db-auth` on the command line) obtains the password at each connection, including Microsoft Entra ID tokens for Azure Database for PostgreSQL with the `azure` extra, which the image now includes. The chart can create a service account and add pod labels, for AKS workload identity. See [Running Marshal on Azure](docs/azure.md).
- The Helm chart runs on OpenShift's restricted SCC (empty `runAsUser` and `fsGroup` are left out), can add a Route and a NetworkPolicy, and the image runs as any UID in group 0. New [security review](docs/security-review.md) for deployment approvals, and notes on locked-down installs.
- The server no longer serves FastAPI's unauthenticated `/docs`, `/redoc` and `/openapi.json` pages, which loaded scripts from a CDN.
- The ingest API accepts OIDC tokens from Jenkins (OpenID Connect Provider plugin) and Azure DevOps pipelines. New settings: `issuers[].owner` and `policy.shipped.workflows`. Signing-key fetches read proxy settings at each fetch and refetch for an unknown key id at most once a minute.

## 0.1.0 (2026-10-26)

The first release.

### Ledger

- A normalized, versioned record of each agent: model, instructions, tools, knowledge sources, permissions and identity. Every value says whether the artifact declared it, a person attested it, a rule derived it, or it is unknown, and why.
- Version hashes that ignore environment-bound values, so the same definition shipped to two environments is one version.
- Proposed, shipped and observed events per environment, with drift between shipped and live, and freshness by channel.
- Risk-ranked diffs: permission escalations, new write tools, identity and model changes first.
- Dependents of any tool, knowledge source or grant target across every platform.
- A SQLite or Postgres store.

### Formats

- Readers for Databricks Asset Bundles, AWS CloudFormation and CDK output (Bedrock Agents, AgentCore), Copilot Studio agent workspaces and exported solutions, Agentforce Agent Script and legacy metadata, Google ADK Agent Config, saved Vertex AI Agent Engine API responses, Python agent code (OpenAI Agents SDK, LangChain and LangGraph, Google ADK, CrewAI, direct model API calls), ADL, Open Agent Spec and AGNTCY OASF.
- Marshal files: hand-written definitions for agents that no reader covers, such as agents on in-house platforms or in vendor UIs with no export. Their values are recorded as `attested` by a person, not as `declared`. `marshal init-agent` writes a commented template.
- Writers for Copilot Studio, Agent Script, Bedrock Agents (CloudFormation), ADK Agent Config, ADL, Agent Spec, OASF and Marshal files, each with a fidelity report computed by reading the output back.
- Portability scores for moving an agent to another platform, from a capability matrix.
- Scope and reversibility derived for standard Microsoft connector operations, from Microsoft's connector reference.

### Running it

- `marshal` command line: `read`, `collect`, `push`, `ls`, `diff`, `status`, `log`, `deps`, `entities`, `convert`, `port`, `init-agent`, `web`, `serve` and `demo`.
- An ingest API that authenticates pipelines with their CI provider's OIDC token (GitHub Actions, GitLab), with repository allowlists and a shipped-event policy.
- A read-only web view behind the organization's SSO proxy.
- A GitHub Action that records events and comments on pull requests with risk-ranked changes.
- Example scheduled workflows that export Copilot Studio and Agentforce agents and report drift.
- A container image for amd64 and arm64, published as `ghcr.io/marshal-ledger/marshal` with an SBOM, build provenance and a Sigstore signature, and a Helm chart.
- Licensed under the Apache License 2.0.
