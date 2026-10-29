---
title: "Getting started with Marshal agent ledger"
---

# Getting started

## See it on real agents

```console
$ pip install 'marshal-ledger[server]'
$ marshal demo
$ marshal web --db demo.sqlite
```

`marshal demo` downloads public agent definitions from GitHub, each pinned to a commit, and records 29 agents from Databricks, Bedrock (CloudFormation and Terraform), AgentCore, Copilot Studio, Agentforce, Google ADK and ADL, with a short history. Open the inventory, then:

- **`agent_openai_agents_sdk/agent_openai_agents_sdk`:** a proposed change that lets the agent run a function that issues refunds, with the grant it needs, both ranked MED.
- **`eval_testAgent`:** a Copilot Studio agent that drifted after one of its tools was switched to the maker's credentials.
- **`mpa_LeaveManagement`:** a Copilot Studio agent read from an exported solution. Its topics read Dataverse and Office 365 as the signed-in user, and it calls a Power Automate flow, so its identity is unknown rather than guessed.
- **`aws_bedrockagent_agent.forex_asst`:** a Bedrock agent read from a Terraform plan of a public example: its action group's tools, its role, and the Lambda function that runs them.
- **Dependencies:** search for `*CheckWeather` to see the Agentforce agents that share an Apex action.
- **Across platforms:** `test_allthethings` has a proposed topic that calls `api.acme.example`. Search for `*api.acme.example/invoices*` to see the ADL agents whose network boundaries allow the same host.

## Read your own agents

Point `marshal read` at an artifact. The format is detected:

```console
$ marshal read path/to/databricks/bundle --target prod
$ marshal read path/to/cloned-copilot-studio-agent
$ marshal read path/to/cdk.out
$ marshal read plan.json                  # Terraform: terraform show -json of a saved plan or of state
$ marshal read path/to/agentcore-project  # AgentCore CLI: agentcore/agentcore.json and its harnesses
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
$ marshal diff my-team/my_agent               # what is proposed against what is shipped, ranked by risk
$ marshal status my-team/my_agent             # shipped and live versions, drift, freshness
$ marshal deps "*my_tool" --shipped           # who depends on a tool, and where it is live
```

## Translate and assess portability

```console
$ marshal convert path/to/agent.adl.yaml --to agent-spec
$ marshal port my-team/my_agent --to all
```

## Put it in your pipelines

Run the [ingest API](ingest-server.md) (see [Deployment](deployment.md)) and add the [GitHub Action](github-action.md) or the [GitLab CI template](gitlab-ci.md) around your deploy step. Pull requests get a risk-ranked comment, deploys are recorded as shipped, and teams never hold store credentials.
