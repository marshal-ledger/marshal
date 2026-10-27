# How Marshal compares

Several kinds of tools touch AI agents, and most organizations run more than one of them. They answer different questions. Marshal answers one: **what is each agent, on every platform, allowed to do, and how has that changed?** It answers it from the agents' definitions, before and after they ship, without connecting to any platform.

| Kind of tool | Answers | Where it sits | With Marshal |
|---|---|---|---|
| A platform's own admin tools | What agents exist on this platform? | Inside one platform | Marshal covers every platform in one record, with versions and diffs |
| Observability and tracing | What did an agent do on each run? | In the agent's runtime | Complementary: runs versus definitions |
| AI gateways | Which model calls are allowed, and at what cost? | In the request path | Complementary: Marshal is never in the request path |
| MCP registries and agent directories | What servers and agents can I discover? | A published catalog | Marshal reads open agent formats, and records what your agents use |
| Infrastructure-as-code scanners | Is this template misconfigured? | In CI, per file | Marshal ranks changes by what they let an agent do, across files and formats |
| AI governance and GRC platforms | Is this use of AI approved, and is its risk assessed? | Questionnaires and workflows | Marshal supplies the facts, from the definitions, through its JSON API |

## A platform's own admin tools

Copilot Studio, Agentforce, Bedrock and Databricks each list their own agents, and some show who built them and what they connect to. Each shows one platform, as it is now. Marshal keeps one normalized record across all of them, with every version, a risk-ranked diff between any two, drift between what was shipped and what is live, and "who depends on this tool?" across platforms, where the definitions name the same API, MCP server or source. It reads exports and repositories, so it needs no access to the platforms' admin APIs.

## Observability and tracing

Tools such as LangSmith, Langfuse, Arize Phoenix and OpenTelemetry's generative AI conventions record what agents do at runtime: each run, prompt, tool call, latency and cost. Marshal records what an agent is configured to do: its model, tools, knowledge, grants and identity. A trace shows that an agent called a refund function; Marshal shows when it was given that function, in which pull request, and ranks that change HIGH or MED. Run both.

## AI gateways

Gateways and proxies such as LiteLLM, Portkey or a cloud API management layer sit between agents and model providers, enforcing which models may be called, rate limits and spend. They see requests, not definitions, and only for traffic routed through them. Marshal is never in the request path, so it adds no latency and can't break an agent, and it sees agents whose traffic never touches a gateway, such as those in Copilot Studio or Agentforce.

## MCP registries and agent directories

The MCP Registry, the AGNTCY Agent Directory and A2A agent cards publish what servers and agents exist and how to reach them, so that clients can discover them. Marshal is the inverse: a private record of what each of your agents actually uses, including MCP servers, and how that changed. It reads and writes the open formats these ecosystems use: [OASF](formats/oasf.md), [Agent Spec](formats/agent-spec.md) and [ADL](formats/adl.md).

## Infrastructure-as-code scanners

Policy-as-code and IaC scanners check each template against rules, such as a role with a wildcard action. They see one file and one format. An agent's power is usually spread across several resources and sometimes several formats: the agent, its action groups, the Lambda behind them and that Lambda's role. Marshal assembles them into one record per agent, and ranks a change by its effect on the agent: a new write grant, a tool switched to the maker's credentials, a different model.

## AI governance and GRC platforms

Governance platforms run the process around AI use: intake questionnaires, risk assessments, approvals, policies and reporting. Most rely on people to describe each system. Marshal gives those processes facts that come from the agents' definitions, each marked as declared, derived, attested or unknown, and keeps them current as agents change. Feed them with `marshal ls --output json` or the web view's JSON API. See [An AI agent inventory for risk and audit reviews](guides/audit-evidence.md).

## What Marshal doesn't do

- It doesn't run, host or orchestrate agents.
- It doesn't watch runtime behavior or block requests.
- It doesn't connect to agent platforms. Definitions arrive as files or as pushes from pipelines you run, so an agent with no export or repository needs a scheduled export you run, or a hand-written [Marshal file](formats/marshal-file.md).
- It doesn't make an organization compliant with a regulation. It supplies evidence for the reviews that do.
