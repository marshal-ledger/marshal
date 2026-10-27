<img src="docs/assets/marshal-logo.svg" width="72" height="72" alt="Marshal logo">

# Marshal — an agent ledger

[![PyPI](https://img.shields.io/pypi/v/marshal-ledger)](https://pypi.org/project/marshal-ledger/)
[![Python](https://img.shields.io/pypi/pyversions/marshal-ledger)](https://pypi.org/project/marshal-ledger/)
[![Tests](https://github.com/marshal-ledger/marshal/actions/workflows/test.yml/badge.svg)](https://github.com/marshal-ledger/marshal/actions/workflows/test.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

A vendor-neutral, open-source ledger for enterprise AI agents. Any format in, any format out, with versions, diffs, portability and audit across every platform.

**[Live demo](https://marshal-ledger.github.io/marshal/demo/)** · **[Documentation](https://marshal-ledger.github.io/marshal/)** · **[Getting started](docs/getting-started.md)**

Marshal reads the agent definitions teams already produce: Databricks bundles, Copilot Studio agents, Bedrock templates, Agentforce metadata, agent code and the open standards. It builds a normalized, versioned record of every agent: what model it asks for, what tools and data it can reach, and who it acts as. Every field says where its value came from, and nothing is guessed.

<img src="docs/assets/demo.gif" alt="A terminal session: marshal demo builds a ledger of 28 public agents, marshal status shows a Copilot Studio agent whose live definition drifted from what was shipped, and marshal diff ranks the switch to the maker's credentials HIGH">

**Status:** alpha. Before 1.0 a minor release may change the ledger model, the ingest API or the command line; the [changelog](CHANGELOG.md) lists each such change.

## What it does

- **Inventory** of every agent across platforms, with versions, deployments, and each record's completeness.
- **Risk-ranked review:** pull requests that change an agent get a comment that lists permission escalations, new write tools and model changes first.
- **Drift and freshness:** compares what is live with what was shipped, and shows how recently each record was confirmed and by which channel.
- **Dependencies:** "who uses this tool, this knowledge source, this permission?" across every platform.
- **Translation** between formats, with a fidelity report computed by reading the result back.
- **Portability scores** for moving an agent to another platform, from a community-maintained capability matrix.
- **No shared secrets:** pipelines push records authenticated by their CI provider's OIDC token. Marshal never connects to agent platforms.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/web-drift-dark.png">
  <img src="docs/assets/web-drift-light.png" alt="An agent page in the Marshal web view: a Copilot Studio agent's live definition differs from what was shipped, and three HIGH changes show its tool switched to the maker's credentials">
</picture>

## Formats

| Source | Read | Write |
|---|---|---|
| Databricks Asset Bundles (Genie spaces, agent apps) | ✓ | |
| AWS CloudFormation and CDK (Bedrock Agents, AgentCore) | ✓ | Bedrock Agents |
| Microsoft Copilot Studio (agent workspaces, solutions) | ✓ | ✓ |
| Salesforce Agentforce (Agent Script, legacy metadata) | ✓ | Agent Script |
| Google ADK Agent Config | ✓ | ✓ |
| Vertex AI Agent Engine (saved API responses) | inventory | |
| Python agent code (OpenAI Agents SDK, LangChain/LangGraph, Google ADK, CrewAI, direct model API calls) | ✓ | |
| ADL, Open Agent Spec, AGNTCY OASF | ✓ | ✓ |
| Marshal file: a hand-written definition for agents no reader covers, recorded as attested | ✓ | ✓ |

## Try it

Browse the [live demo](https://marshal-ledger.github.io/marshal/demo/), or build it yourself from 28 public agents, with nothing to install but [uv](https://docs.astral.sh/uv/):

```console
$ uvx --from 'marshal-ledger[server]' marshal demo
$ uvx --from 'marshal-ledger[server]' marshal web --db demo.sqlite
```

Or `pip install 'marshal-ledger[server]'`, then `marshal demo` and `marshal web --db demo.sqlite`. `marshal demo` downloads the agents' definitions from their public GitHub repositories, pinned to a commit.

Then on your own agents. See [Getting started](docs/getting-started.md):

```console
$ marshal read path/to/bundle                        # print ledger records
$ marshal collect path/to/bundle --target prod --event shipped
$ marshal ls                                         # every agent, latest version, where it is shipped
$ marshal diff finance/invoice_agent                 # proposed against shipped, ranked by risk
$ marshal status finance/invoice_agent               # shipped vs live per environment, drift, freshness
$ marshal deps "*CheckWeather" --shipped             # who depends on this, and where it is live
$ marshal convert agent.adl.yaml --to agent-spec     # translate, with a fidelity report
$ marshal port finance/invoice_agent --to all        # portability to every platform
$ marshal web                                        # browse the store
```

`collect` writes to a local SQLite store (`marshal.sqlite`, or `--db` / `$MARSHAL_DB`). In a shared deployment, pipelines run `marshal push` or the [GitHub Action](docs/github-action.md) against the [ingest API](docs/ingest-server.md). Optional [scheduled workflows](examples/scheduled/) export agents built in a web designer, such as Copilot Studio and Agentforce, and report drift from what was shipped.

## Documentation

The documentation is published at [marshal-ledger.github.io/marshal](https://marshal-ledger.github.io/marshal/).

- [Getting started](docs/getting-started.md)
- [Design](docs/design.md): the problem, architecture, security and network posture, roadmap
- [Ledger model v0](docs/ledger-model.md): records, provenance, version hashing, the version store, risk ranking, freshness
- [Conversion and fidelity](docs/conversion.md) and [Portability](docs/portability.md)
- Formats: [Databricks](docs/formats/databricks.md), [CloudFormation](docs/formats/cloudformation.md), [Copilot Studio](docs/formats/copilot-studio.md), [Agentforce](docs/formats/agentforce.md), [ADK Agent Config](docs/formats/adk-config.md), [Vertex AI Agent Engine](docs/formats/vertex-agent-engine.md), [Python agent code](docs/formats/python.md), [ADL](docs/formats/adl.md), [Agent Spec](docs/formats/agent-spec.md), [OASF](docs/formats/oasf.md), [Marshal file](docs/formats/marshal-file.md)
- [Platform exportability](docs/platform-exportability.md): what each closed platform exposes
- [Ingest API](docs/ingest-server.md), [GitHub Action](docs/github-action.md), [Jenkins and Azure DevOps](docs/other-ci.md) and [Deployment](docs/deployment.md)
- [Security review](docs/security-review.md): what is stored, authentication, network, supply chain, retention and audit, for security reviewers

## Development

```console
$ pip install -e '.[dev]'
$ python scripts/fetch_fixtures.py     # golden fixtures: real public agent definitions, pinned by commit
$ pytest
```

See [Contributing](CONTRIBUTING.md). Golden fixtures are fetched rather than committed, so each file stays under its upstream license. After changing the model, regenerate the schema with `python scripts/generate_schema.py`. Report vulnerabilities as described in [Security](SECURITY.md).

## License

Marshal is licensed under the [Apache License 2.0](LICENSE).
