# Getting started

## See it on real agents

```console
$ pip install 'marshal-ledger[server]'
$ marshal demo
$ marshal web --db demo.sqlite
```

`marshal demo` downloads public agent definitions from GitHub, each pinned to a commit, and records 28 agents from Databricks, Bedrock, AgentCore, Copilot Studio, Agentforce, Google ADK and ADL, with a short history. Open the inventory, then:

- **`agent_openai_agents_sdk/agent_openai_agents_sdk`:** a proposed change that adds a write tool, ranked HIGH.
- **`eval_testAgent`:** a Copilot Studio agent that drifted after one of its tools was switched to the maker's credentials.
- **`mpa_LeaveManagement`:** a Copilot Studio agent read from an exported solution. Its topics read Dataverse and Office 365 as the signed-in user, and it calls a Power Automate flow, so its identity is unknown rather than guessed.
- **Dependencies:** search for `*CheckWeather` to see the Agentforce agents that share an Apex action.

## Read your own agents

Point `marshal read` at an artifact. The format is detected:

```console
$ marshal read path/to/databricks/bundle --target prod
$ marshal read path/to/cloned-copilot-studio-agent
$ marshal read path/to/cdk.out
$ marshal read path/to/agent/repo          # Python code: OpenAI Agents SDK, LangChain, ADK, CrewAI, or direct API calls
```

Each record shows where every value came from (`declared`, `attested`, `derived`, `annotation` or `unknown`), and how complete the record is.

For an agent no reader covers, such as one on an in-house platform or in a vendor UI with no export, write its definition by hand in a [Marshal file](formats/marshal-file.md). Its values are recorded as attested by a person, not as declared by the platform:

```console
$ marshal init-agent triage.marshal.yaml --id ops/triage --platform in-house
$ marshal read triage.marshal.yaml
```

## Keep a history

```console
$ marshal collect path/to/bundle --target prod --event shipped
$ marshal collect path/to/bundle --target prod --event proposed
$ marshal diff finance/invoice_agent          # what is proposed against what is shipped, ranked by risk
$ marshal status finance/invoice_agent        # shipped and live versions, drift, freshness
$ marshal deps "*update_po" --shipped         # who depends on a tool, and where it is live
```

## Translate and assess portability

```console
$ marshal convert path/to/agent.adl.yaml --to agent-spec
$ marshal port finance/invoice_agent --to all
```

## Put it in your pipelines

Run the [ingest API](ingest-server.md) (see [Deployment](deployment.md)) and add the [GitHub Action](github-action.md) around your deploy step. Pull requests get a risk-ranked comment, deploys are recorded as shipped, and teams never hold store credentials.
