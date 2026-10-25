# Marshal — an agent ledger

A vendor-neutral, open-source ledger for enterprise AI agents. Any format in, any format out, with versions, diffs, portability and audit across every platform.

Marshal reads the agent definitions teams already produce, such as Databricks bundles, Copilot Studio agents, Bedrock templates, Agentforce metadata and open standards. It builds a normalized, versioned record of every agent: what model it asks for, what tools and data it can reach, and who it acts as. Every field says where its value came from, and nothing is guessed.

**Status:** pre-alpha. The ledger model and the Databricks Asset Bundle reader are implemented.

## Try it

```console
$ pip install -e '.[dev]'
$ marshal read path/to/databricks/bundle
$ marshal read path/to/databricks/bundle --target prod --hash-only
```

`marshal read` prints one ledger record per agent as YAML (or JSON with `--output json`). Items the reader looked at and did not record as agents are listed on stderr with the reason.

## Documentation

- [Design](docs/design.md): the problem, architecture, roadmap
- [Ledger model v0](docs/ledger-model.md): records, provenance, version hashing, completeness
- [Platform exportability](docs/platform-exportability.md): what Copilot Studio, Bedrock, Vertex AI, Agentforce and Databricks expose
- [Databricks reader](docs/readers/databricks.md): mapping rules and known gaps

## Development

```console
$ pip install -e '.[dev]'
$ python scripts/fetch_fixtures.py     # golden fixtures: real public agent definitions, pinned by commit
$ pytest
```

Golden fixtures are fetched rather than committed, so each file stays under its upstream license. Golden tests skip when the fixtures are absent. After changing the model, regenerate the schema with `python scripts/generate_schema.py`.
