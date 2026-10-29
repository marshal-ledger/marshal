---
title: "Marshal: open-source AI agent inventory for Copilot Studio, Agentforce, Bedrock and Databricks"
description: >-
  A self-hosted AI agent inventory with versions, risk-ranked diffs, drift and dependencies across Copilot Studio,
  Agentforce, Bedrock, Databricks and agent code. Runs inside your network; no AI, no platform credentials.
hide:
  - navigation
---

# Marshal — an agent ledger

Marshal is an open-source AI agent inventory. It keeps one versioned record of every AI agent in an organization, whatever platform it was built on. It reads the agent definitions teams already produce: Databricks bundles, Copilot Studio agents, Bedrock templates, Agentforce metadata, agent code and the open standards. For each agent it records what model it asks for, what tools and data it can reach, and who it acts as. Every value says where it came from, and nothing is guessed.

[Try the live demo](https://marshal-ledger.github.io/marshal/demo/){ .md-button .md-button--primary }
[Get started](getting-started.md){ .md-button }
[GitHub](https://github.com/marshal-ledger/marshal){ .md-button }

<video class="marshal-video" controls preload="none" poster="assets/marshal-short-poster.png" aria-label="A short video: a platform engineer is asked which AI agents her company runs, finds them spread across four platforms with uneven controls, and uses Marshal to build the inventory and catch a change that removes a guardrail before it ships">
  <source src="assets/marshal-short.mp4" type="video/mp4">
</video>

A short story (1:44): Maya, a fictional platform engineer, has to tell her security team which AI agents her company runs. The survey figures cite their sources on screen, and the Marshal screens are real output. Music: "Bossa Nova Cafe Morning Breeze" by alex-morgan, under the [Pixabay Content License](https://pixabay.com/service/license-summary/).
{ .marshal-video-caption }

![A walk through the Marshal web view: the inventory of 29 public agents, a Copilot Studio agent in drift whose tool was switched to the maker's credentials, a proposed change that adds a refund function, and the agents that share an Apex action](assets/web-tour.gif)

## What it does

- **Inventory** of every agent across platforms, with versions, deployments, and each record's completeness.
- **Risk-ranked review:** pull requests that change an agent get a comment that lists permission escalations, new write tools and removed guardrails first.
- **Drift and freshness:** compares what is live with what was shipped, and shows how recently each record was confirmed and by which channel.
- **Dependencies:** "who uses this tool, this knowledge source, this permission?" Refs on other platforms that reach the same HTTP operation, MCP server, knowledge URL or API host are listed too, each with how it matched.
- **Translation** between formats, with a fidelity report computed by reading the result back.
- **Portability scores** for moving an agent to another platform, from a community-maintained capability matrix.

![The Marshal web view: an inventory of 29 agents across Agentforce, Databricks, Bedrock, Copilot Studio and Google ADK, with one environment in drift](assets/web-inventory-light.png#only-light)
![The Marshal web view: an inventory of 29 agents across Agentforce, Databricks, Bedrock, Copilot Studio and Google ADK, with one environment in drift](assets/web-inventory-dark.png#only-dark)

## Easy to approve

Most tools that touch AI agents raise questions in a security review. Marshal is built so it doesn't:

- **It runs inside your network.** You host it, on your own database. There is no Marshal cloud service, so the agent definitions, instructions and permissions it records never go to a vendor. The service's only outbound requests fetch your CI provider's public signing keys.
- **It uses no AI.** Marshal reads definitions with ordinary parsers and fixed, documented rules. It never sends your agents' instructions, tools or permissions to a language model, so there is no AI system to evaluate before you approve it.
- **It needs no platform credentials.** Your existing CI pipelines push definitions to Marshal, signed in with their own short-lived OIDC tokens. Marshal never connects to Copilot Studio, Bedrock or any other platform where your agents live, so it holds no keys to them.

The [security review](security-review.md) covers what Marshal stores, what it connects to, and how releases are signed.

## How it runs

![How Marshal runs: agent definitions in repositories and web designers reach your CI pipelines, which push proposed, shipped and observed events to Marshal's ingest API with their OIDC token. Marshal records them in the ledger, and teams see risk-ranked pull request comments, the web view and the CLI. Marshal never connects to an agent platform.](assets/architecture.svg)

Pipelines you already run record each agent: a pull request records the proposed definition and gets a comment ranking its changes, a deploy records what was shipped, and an optional scheduled export records what is live in a web designer. See the [GitHub Action](github-action.md), [GitLab CI](gitlab-ci.md), [Jenkins and Azure DevOps](other-ci.md) and [Deployment](deployment.md).

## Try it in a minute

```console
$ uvx --from 'marshal-ledger[server]' marshal demo
$ uvx --from 'marshal-ledger[server]' marshal web --db demo.sqlite
```

Or install it with `pip install 'marshal-ledger[server]'`. `marshal demo` downloads 29 public agent definitions from GitHub, each pinned to a commit, and records them with a short history: two proposed changes, and a Copilot Studio agent that drifted after an edit in its web designer.

![An agent page in the web view: the Copilot Studio agent's live definition differs from what was shipped, and three HIGH changes show its tool switched to the maker's credentials](assets/web-drift-light.png#only-light)
![An agent page in the web view: the Copilot Studio agent's live definition differs from what was shipped, and three HIGH changes show its tool switched to the maker's credentials](assets/web-drift-dark.png#only-dark)

## Formats

| Source | Read | Write | Writer tested by |
|---|---|---|---|
| [Databricks Asset Bundles](formats/databricks.md) (Genie spaces, agent apps) | ✓ | |  |
| [AWS CloudFormation and CDK](formats/cloudformation.md) (Bedrock Agents, AgentCore harnesses and runtimes) | ✓ | Bedrock Agents, AgentCore harness | cfn-lint; Bedrock Agents deployed |
| [Terraform](formats/terraform.md) plan and state (Bedrock Agents, AgentCore harnesses and runtimes; AWS and Cloud Control providers) | ✓ | | |
| [AgentCore CLI](formats/agentcore-cli.md) projects (AgentCore harnesses) | ✓ | | |
| [Microsoft Copilot Studio](formats/copilot-studio.md) (agent workspaces, solutions) | ✓ | ✓ | published schema, partly |
| [Salesforce Agentforce](formats/agentforce.md) (Agent Script, legacy metadata) | ✓ | Agent Script | round trip |
| [Google ADK Agent Config](formats/adk-config.md) | ✓ | ✓ | published schema; loads in ADK |
| [Vertex AI Agent Engine](formats/vertex-agent-engine.md) (saved API responses) | inventory | |  |
| [Python agent code](formats/python.md) (OpenAI Agents SDK, LangChain/LangGraph, Google ADK, CrewAI, direct model API calls) | ✓ | |  |
| [ADL](formats/adl.md), [Open Agent Spec](formats/agent-spec.md), [AGNTCY OASF](formats/oasf.md) | ✓ | ✓ | round trip; published schema for ADL and Agent Spec |
| [Marshal file](formats/marshal-file.md): a hand-written definition for agents no reader covers | ✓ | ✓ | round trip |

Readers are tested against real, public definitions, pinned to a commit. Only the Bedrock writer's output has been deployed to its platform so far; see [how each format is verified](design.md#formats-and-how-each-is-verified), and [supported versions](formats/supported-versions.md) for the variants each reader handles.

## Where to next

- Guides: [inventory Copilot Studio agents](guides/copilot-studio-inventory.md), [review agent changes in pull requests](guides/pull-request-review.md), [track agents in code](guides/agents-in-code.md), [detect drift](guides/drift-detection.md), [convert an ADK agent to Agent Spec](guides/convert-adk-to-agent-spec.md), [move Bedrock agents to the AgentCore harness](guides/bedrock-agents-to-agentcore.md), and [an agent inventory for audit](guides/audit-evidence.md).
- [How Marshal compares](comparison.md) with cloud agent registries, platform admin tools, observability, AI gateways and governance platforms.
- [Getting started](getting-started.md): read your own agents and record their history.
- [Design](design.md): the problem, the architecture, and the security and network posture.
- [GitHub Action](github-action.md), [GitLab CI](gitlab-ci.md), [Jenkins and Azure DevOps](other-ci.md): record agents from CI and comment on pull and merge requests.
- [Deployment](deployment.md) and the [security review](security-review.md): run the ingest API and web view for a whole organization.
