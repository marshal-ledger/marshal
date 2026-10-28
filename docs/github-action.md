---
title: "GitHub Action: record AI agents and review changes in pull requests"
---

# GitHub Action

`marshal-ledger/marshal/collect` reads agent definitions from the checked-out repository, sends them to the Marshal ingest API, and on pull requests posts the risk-ranked changes as a comment. Later runs on the same pull request update the comment in place.

It runs on GitHub-hosted runners, and on self-hosted runners or GitHub Enterprise Server with runner v2.327.1 or later, which run Node 24 actions. The job needs these permissions:

```yaml
permissions:
  contents: read
  id-token: write        # OIDC token for Marshal; no stored secret
  pull-requests: write   # only for the PR comment
```

A complete workflow is in [`examples/github-workflow.yml`](../examples/github-workflow.yml). The pattern is to record `proposed` on pull requests and `shipped` after the deploy step succeeds:

```yaml
- uses: marshal-ledger/marshal/collect@v0
  with:
    event: proposed
    artifacts: .
    target: prod
    url: https://marshal.acme.example
```

`@v0` follows the latest 0.x release. To decide when to upgrade, pin a release tag such as `@v0.8.0`, or a commit SHA. The Action installs Marshal from the same commit, so the client always matches the Action's version.

| Input | Required | Meaning |
|---|---|---|
| `event` | yes | `proposed`, `shipped`, or `observed` for a [scheduled export](../examples/scheduled/) of a live definition |
| `artifacts` | yes | Path of the agent artifact, such as a bundle directory, or the JSON of a [Terraform](formats/terraform.md#in-a-pipeline) plan |
| `url` | yes | Base URL of the ingest API |
| `target` | no | Deployment target whose overrides to apply |
| `environment` | no | Environment the event applies to; defaults to `target` |
| `audience` | no | OIDC audience (default `marshal`) |
| `comment` | no | Comment on pull requests (default `true`) |
| `portability` | no | Space-separated platforms to score portability to in the comment, such as `bedrock copilot-studio` |
| `python-version` | no | Python used to run Marshal (default `3.12`) |

The report is also added to the job summary and exposed as the `report` output.

A typical comment:

<!-- example: pr-comment-maker-credentials -->
```
**eval_testAgent** (copilot-studio) · v1 → v2 · 3 changes, 3 high risk

HIGH  ~ permissions.identity: mixed → shared_credential
HIGH  ~ extensions.copilot-studio.actions.SearchDocs.connection_mode: Invoker → Maker
HIGH  ~ extensions.copilot-studio.actions.SearchDocs.credentials: end user → maker
Completeness 38%
```

Pull requests from forks don't receive OIDC tokens, so the action can't record events for them.

## Runners without internet access

The Action has three steps that reach beyond the runner, so a self-hosted runner without internet access needs each one served from inside your network:

1. **Python.** The Action runs `actions/setup-python`, pinned to a commit, with `python-version` (default `3.12`). Without access to github.com, setup-python takes Python only from the runner's tool cache (`RUNNER_TOOL_CACHE/Python`). Populate it as GitHub describes in [Setting up the tool cache on self-hosted runners without internet access](https://docs.github.com/en/enterprise-server/admin/github-actions/managing-access-to-actions-from-githubcom/setting-up-the-tool-cache-on-self-hosted-runners-without-internet-access), with a version that matches `python-version`.
2. **Marshal.** The Action installs Marshal from its own checkout with `pip install`, which builds it from source. pip reads its index from `PIP_INDEX_URL` or `pip.conf` on the runner (see [Locked-down installs](deployment.md#locked-down-installs)), and the build uses the same index. Besides `pydantic` and `pyyaml`, the index must serve the build backends `hatchling` and `hatch-fancy-pypi-readme`, and their dependencies. A pull-through proxy of PyPI serves them on demand; a curated mirror must list them. On a connected machine, `pip download hatchling hatch-fancy-pypi-readme pydantic pyyaml` fetches the whole set for that machine's Python and platform.
3. **Other Actions.** On GitHub Enterprise Server without GitHub Connect, sync `marshal-ledger/marshal` and `actions/setup-python`, at the commit the Action pins, to the appliance with `actions-sync`.

The runner also needs to reach the Marshal ingest URL and your GitHub host, which issues the OIDC token and, for the pull request comment, serves the API. The comment step runs the `gh` CLI, so a self-hosted runner needs it installed, or set `comment: "false"`.

If your runners already have Python and an internal index, you can skip the Action and run the client directly: `pip install marshal-ledger`, then `marshal push <artifacts> --event proposed --target prod --url <ingest URL> --commit "$GITHUB_SHA"`, in a job with `id-token: write`. That installs the published wheel, which needs no build backends.
