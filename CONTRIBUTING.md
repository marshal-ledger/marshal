# Contributing

Thanks for helping. The most useful contributions are corrections to the [capability matrix](src/marshal_ledger/capabilities/README.md), new public golden fixtures, and readers and writers for formats Marshal doesn't support yet.

## Development

```console
$ pip install -e '.[dev]'
$ python scripts/fetch_fixtures.py
$ pytest
```

To run the store tests against Postgres, set `MARSHAL_TEST_POSTGRES_URL` to an empty database you can reset.

## Adding a format

A format has a reader (`src/marshal_ledger/readers/`), usually a writer (`src/marshal_ledger/writers/`), and a page in `docs/formats/`.

- **Read only what the artifact states.** A value the artifact doesn't give is `unknown`, with a `basis` saying why. A value computed by a rule is `derived`, with the rule as its basis.
- **Never copy credentials.** Copy fields from an allowlist. Record environment variables by name, never by value. Pass free text through `redact`.
- **Keep environment-bound values in `bindings`,** so the same definition deployed to two environments is one version.
- **Keep everything the ledger model can't express** under `extensions.<format>`, so a native round trip is lossless.
- **Declare a writer's losses** in its `LOSSES` table. Explain format-specific ones with notes. The translation tests fail on any loss without a reason.

## Golden fixtures

Golden fixtures are real, public agent definitions, never synthetic ones. Add them to [`tests/fixtures/sources.json`](tests/fixtures/sources.json), pinned to a commit. They are fetched rather than committed, so each file stays under its own license. Check the license before adding a source.

## Standards for what is committed

Everything in the repository is written for users and contributors. Keep personal notes and to-do lists out of commits.
