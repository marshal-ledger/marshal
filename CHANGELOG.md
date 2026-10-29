# Changelog

Notable changes to Marshal, newest first. Versions follow [Semantic Versioning](https://semver.org). Before 1.0, a minor release may change the ledger model, the ingest API or the command line; each such change is listed here.

## 0.9.0 (unreleased)

- AgentCore harnesses: the CloudFormation reader reads `AWS::BedrockAgentCore::Harness` in full: the model and its provider, the system prompt, and the tools, including each tool of a gateway defined in the same template with the Lambda that runs it, and tools the model provider runs, such as OpenAI's web search. A harness whose `AllowedTools` doesn't restrict them is recorded with the default `shell` and `file_operations` tools, so an agent that can run commands shows as one. MCP header and environment variable values are never copied, only their names.
- A new `agentcore-harness` writer writes an agent as an AgentCore harness in CloudFormation, checked by cfn-lint against AWS's published harness schema. It lists the agent's tools in `AllowedTools`, so a written harness never gets a shell its source didn't have, and it writes a Bedrock agent's guardrail as the model's guardrail configuration. Not yet deployed to AgentCore. AWS put Bedrock Agents in maintenance mode on July 30, 2026 and recommends the harness; the new guide [Move Bedrock agents to the AgentCore harness](docs/guides/bedrock-agents-to-agentcore.md) walks through a conversion and what doesn't carry over.
- AgentCore CLI projects: a new `agentcore-cli` reader reads the harnesses of an AgentCore CLI project (`agentcore/agentcore.json`, each harness's `harness.yaml` or the older `harness.json`, and `system-prompt.md`). Each harness is translated into its CloudFormation resource and read with the same mapping, so it gets the same record and version as in CloudFormation, and `harness.json` and `harness.yaml` give the same version. A harness without an execution role is recorded with the role `agentcore deploy` creates. See [AgentCore CLI projects](docs/formats/agentcore-cli.md).
- Terraform: the reader reads AgentCore harnesses defined with the Cloud Control provider (`awscc_bedrockagentcore_harness`), with the same mapping as CloudFormation. A field of a Cloud Control object that is known only after apply, but that nothing in its expression can fill, is recorded as absent: it is an optional field the provider computes, such as a harness model's `top_p`.
- Harnesses: a model's free-form parameters (`AdditionalParams`, used by LiteLLM) keep a parameter whose name suggests a credential, such as `api_key` or `extra_headers`, by name only, and other text in them is redacted.
- The `agentcore-harness` writer turns a memory that a CLI project names into a template parameter for its ARN. The CloudFormation (Bedrock Agents) writer no longer fails on a parameter whose JSON Schema type is a list, such as `["string", "null"]`, and the Agent Spec writer explains schema-level keywords it doesn't keep, such as `additionalProperties`. Real CLI projects found all three.
- `marshal convert --migrate` moves an agent to the target's platform: it keeps its id, so its history continues, and is recorded on the new platform.
- `marshal port` scores moves to the AgentCore harness (`bedrock-agentcore`).
- Risk ranking: a change to a harness's hooks, its gateway outbound auth or its MCP headers ranks HIGH, and a change to its allowed tools MED.
- `marshal port` printed some advice and labels cut off at the first comma, such as Bedrock's advice for MCP servers, because the capability files left them unquoted. They are quoted, and a rule with an unknown key now fails to load.
- Documentation: the homepage has a short video that follows a platform engineer from an audit question to an agent inventory and a pull request comment that flags a removed guardrail. The source archive leaves it out, as it does the site's images.
- Tests: cfn-lint 1.59 warns on every `AWS::Bedrock::Agent` that Bedrock Agents is in maintenance mode (W3697). The CloudFormation writer's templates are still linted; that one note about the service is set aside, and the CloudFormation format page says so.

## 0.8.0 (2026-10-27)

- The GitLab CI template has been tested on GitLab.com: shipped and proposed events, and one merge request comment updated on later pushes.
- Web view: page titles name the agent and its platform, and each page has a description. `marshal web --export DIR --site-url URL` also writes a `sitemap.xml` listing the inventory, the dependencies page and each agent's latest version.
- The Copilot Studio writer writes a knowledge URL as a SharePoint source only when its host is on `sharepoint.com`. It checked whether the URL contained `.sharepoint.com` anywhere, so a public site whose path or query mentioned it was written as SharePoint.
- Documentation: page titles and descriptions say what each page covers, such as "Bedrock agent inventory from Terraform plans and state". The comparison page covers the cloud agent registries: AWS Agent Registry, Microsoft Agent 365, Google Cloud Agent Registry and MuleSoft Agent Fabric.

## 0.7.1 (2026-10-27)

- GitLab CI: `marshal comment` sends its token in the `Authorization: Bearer` header, which GitLab accepts for every kind of token. It used `PRIVATE-TOKEN`, which refuses OAuth tokens, so a merge request comment failed with 401 when the variable held one. Found by running the template on GitLab.com.

## 0.7.0 (2026-10-27)

- GitLab CI: a template (`collect/gitlab-ci.yml`) to include from a release tag and extend, which records events with a GitLab ID token and comments on merge requests with the risk-ranked changes, updating one comment on later pushes. A new `marshal comment` command posts the comment; it needs a GitLab token (a project access token, or on GitLab.com Free a bot account's personal access token), and without one the report stays in the job log and artifacts. `MARSHAL_PACKAGE` installs Marshal from somewhere other than PyPI, such as a mirror. See the new GitLab CI page.
- The README and documentation say why Marshal is easy to approve: it runs inside your network, uses no AI, and needs no platform credentials. The architecture diagram's footnote keeps even spacing in every font.

## 0.6.0 (2026-10-27)

- Terraform: a new `terraform` reader reads `terraform show -json` of a saved plan or of state. It covers Bedrock agents defined with the AWS provider (`aws_bedrockagent_agent` and its action group, knowledge base, collaborator and alias resources) or the Cloud Control provider (`awscc_bedrock_agent`), and AgentCore runtimes. Values known only after apply are followed to their resources across module inputs and outputs; sensitive values are never copied. Agents are mapped by the CloudFormation reader's Bedrock mapping, so the same agent defined either way gets the same version.
- The Terraform reader has been tested against a real plan and state: a public example planned and applied in an AWS account gave the same version from both.
- The Terraform reader warns when a value it needs comes through a `local`, which a plan doesn't record, and says what that leaves out, such as a role policy's permissions or a collaborator's link to its agent.
- The CloudFormation writer has been deployed to Amazon Bedrock: a written template deployed unchanged and the agent Bedrock stored matched the source. The writer now warns when the source doesn't say whether a function needs confirmation, since Bedrock then stores it as not needing one.
- Bedrock: a value equal to Bedrock's default (idle session timeout 600, agent collaboration `DISABLED`, orchestration type `DEFAULT`) is recorded as absent, so a Terraform plan and the state after apply give the same version, and so does a CloudFormation template whether it states the default or not. An agent whose template states one of these defaults gets a new version once.
- The CloudFormation writer writes a knowledge base defined in the source itself as a template parameter, since it has no id until it is deployed.
- `marshal demo` adds a Bedrock agent read from a Terraform plan of a public example, for 29 agents.

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
