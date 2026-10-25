# Marshal — an agent ledger

A vendor-neutral, open-source ledger for enterprise AI agents. Any format in, any format out, with versions, diffs, portability and audit across every platform.

Marshal reads the agent definitions teams already produce, such as Databricks bundles, Copilot Studio agents, Bedrock templates, Agentforce metadata and open standards. It builds a normalized, versioned record of every agent: what model it asks for, what tools and data it can reach, and who it acts as. Every field says where its value came from, and nothing is guessed.

**Status:** pre-alpha. Implemented so far: the ledger model, the Databricks Asset Bundle reader, the version store, risk-ranked diffs, the ingest API with OIDC authentication, and the GitHub Action.

## Try it

```console
$ pip install -e '.[dev]'
$ marshal read path/to/bundle                        # print ledger records
$ marshal collect path/to/bundle --target prod --event shipped
$ marshal collect path/to/bundle --target prod --event proposed
$ marshal ls                                         # every agent, latest version, where it is shipped
$ marshal ls --tool "main.finance.*"                 # agents that use matching tools
$ marshal diff finance/invoice_agent                 # latest against last shipped
$ marshal diff finance/invoice_agent@v3 finance/invoice_agent@v5
$ marshal log finance/invoice_agent                  # proposed and shipped events
```

`collect` writes to a local SQLite store (`marshal.sqlite`, or `--db` / `$MARSHAL_DB`). In a shared deployment, pipelines run `marshal push` (or the [GitHub Action](docs/github-action.md)) against the [ingest API](docs/ingest-server.md), authenticated by their CI provider's OIDC token, so agent teams never hold store credentials.

## Documentation

- [Design](docs/design.md): the problem, architecture, roadmap
- [Ledger model v0](docs/ledger-model.md): records, provenance, version hashing, the version store, completeness
- [Platform exportability](docs/platform-exportability.md): what Copilot Studio, Bedrock, Vertex AI, Agentforce and Databricks expose
- [Databricks reader](docs/readers/databricks.md): mapping rules and known gaps
- [Ingest API](docs/ingest-server.md): running the server, OIDC issuers, shipped policy, agent ownership
- [GitHub Action](docs/github-action.md): recording events and PR comments

## Development

```console
$ pip install -e '.[dev]'
$ python scripts/fetch_fixtures.py     # golden fixtures: real public agent definitions, pinned by commit
$ pytest
```

Golden fixtures are fetched rather than committed, so each file stays under its upstream license. Golden tests skip when the fixtures are absent. After changing the model, regenerate the schema with `python scripts/generate_schema.py`.
