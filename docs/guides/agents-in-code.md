# Track LangGraph, CrewAI and OpenAI Agents SDK agents from code

Agents written in code don't appear in any admin console. Their model, tools and prompts are spread across Python modules, and they change with every commit. Marshal reads the code statically, the same way a linter does, and records each agent it finds.

## Read a repository

```console
$ pip install marshal-ledger
$ marshal read path/to/repo
```

Each agent constructor is one agent: `agents.Agent` in the OpenAI Agents SDK, `create_agent` and `create_react_agent` in LangChain and LangGraph, `Agent` and `LlmAgent` in Google ADK, and `crewai.Agent`, including agents configured in `config/agents.yaml`. Code that calls a model API directly with tools (OpenAI, Anthropic, Google GenAI, LiteLLM, Bedrock Runtime) is recorded too, marked as inferred. See [Python agent code](../formats/python.md) for the full list.

**The code is never imported or run.** Files are parsed, and only what can be known without running anything is resolved: literals, f-strings, names in the same module, imports within the project, and prompt files read from disk. Anything else, such as a model name read from an environment variable, is recorded as `unknown` with the reason.

## What a record looks like

From the OpenAI Agents SDK's customer service example:

```yaml
agent: customer_service/faq-agent
platform: openai-agents
source:
  artifact: main.py
  locator: agents.Agent at line 94
spec:
  display_name:
    value: FAQ Agent
    provenance: declared
  model:
    requested:
      provenance: unknown
      basis: the SDK's default model is used
  tools:
  - ref: tool/python.customer_service/main:faq_lookup_tool
    name: faq_lookup_tool
    description: Lookup frequently asked questions.
```

Every value says where it came from: `declared` by the code, `derived` by a rule, or `unknown`, with a basis. The completeness score tells you how much of the agent the code actually states.

## Record every change

Record the agents on each merge, and review changes on pull requests, like any other format:

```console
$ marshal collect path/to/repo --event shipped --environment prod
$ marshal ls
AGENT                                PLATFORM       LATEST  SHIPPED  FRESHNESS        COMPLETE
crewai-template/reporting-analyst    crewai         v1      prod v1  prod: manual 0d  30%
crewai-template/researcher           crewai         v1      prod v1  prod: manual 0d  30%
customer_service/faq-agent           openai-agents  v1      prod v1  prod: manual 0d  20%
customer_service/seat-booking-agent  openai-agents  v1      prod v1  prod: manual 0d  20%
customer_service/triage-agent        openai-agents  v1      prod v1  prod: manual 0d  15%
```

In CI, the [GitHub Action](../github-action.md) or `marshal push` does the same against a shared server, and comments on pull requests that give an agent a new tool or a different model. See [Review agent changes in pull requests](pull-request-review.md).

Tool refs are scoped to the project, `tool/python.<project>/<module>:<function>`, so two copies of the same template don't merge into one tool. To find every agent that uses a function:

```console
$ marshal deps "tool/python.*:issue_refund"
```
