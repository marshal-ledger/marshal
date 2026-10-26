# Contributing

Thanks for helping. The most useful contributions are corrections to the [capability matrix](src/marshal_ledger/capabilities/README.md), new public golden fixtures, and readers and writers for formats Marshal doesn't support yet.

Marshal is licensed under the [Apache License 2.0](LICENSE). By contributing, you agree that your contributions are licensed under it too, as its section 5 describes.

## Development

Changes reach `main` through pull requests, and the CI checks must pass before merging.

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

## Keeping up with upstream formats

Agent formats change upstream. A weekly workflow ([`upstream.yml`](.github/workflows/upstream.yml)) checks two things:

- **Surfaces.** [`upstream/watch.json`](upstream/watch.json) lists the published definitions the readers depend on: the Databricks bundle schema, AWS's CloudFormation schemas for Bedrock and AgentCore, the Copilot Studio authoring schema, Agent Script keywords, the ADL, Agent Spec and OASF schemas, ADK Agent Config, and the agent frameworks' minor releases. `scripts/check_upstream.py` extracts each one's surface (field, type and keyword names) and compares it with [`upstream/snapshots/`](upstream/snapshots/).
- **Latest fixtures.** The golden fixtures are fetched from each source's latest commit instead of the pinned one, and the golden tests run against them.

Each change opens or updates one issue labelled `upstream`. To resolve it:

1. Update the reader or writer, and add a golden fixture for any new construct.
2. Accept the new surface with `python scripts/check_upstream.py --update`.
3. Move the fixture's pinned commit forward, in the same pull request.

The issue closes on the next run. To check locally, run `python scripts/check_upstream.py`.

## Releasing

The package version is `__version__` in [`src/marshal_ledger/__init__.py`](src/marshal_ledger/__init__.py). Between releases it ends in `.dev0`, and [`CHANGELOG.md`](CHANGELOG.md) collects changes under the next version, marked `(unreleased)`.

1. In a pull request, set `__version__` and the chart's `appVersion` to the release, such as `0.2.0`, and bump the chart's own `version`. Replace `(unreleased)` with the date (`YYYY-MM-DD`) and update the version in the docs; `tests/unit/test_release.py` lists where.
2. Once it's merged, tag the merge commit on `main` and push the tag: `git tag v0.2.0 && git push origin v0.2.0`. Release tags can only be created by maintainers and can't be moved or deleted.
3. The [release workflow](.github/workflows/release.yml) checks the versions and the changelog, runs the tests, builds the wheel, sdist and chart, publishes a GitHub release, and moves the major tag (`v0`) that the GitHub Action is used by. It publishes to PyPI and pushes a container image only where those are configured; the workflow's header explains how.
4. Set `__version__` to the next `.dev0` version and start its changelog section.

Before 1.0, a minor release may change the ledger model, the ingest API or the command line. List each such change in the changelog.

## Standards for what is committed

Everything in the repository is written for users and contributors. Keep personal notes and to-do lists out of commits.
