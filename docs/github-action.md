# GitHub Action

`marshal-ledger/marshal/collect` reads agent definitions from the checked-out repository, sends them to the Marshal ingest API, and on pull requests posts the risk-ranked changes as a comment. Later runs on the same pull request update the comment in place.

The job needs these permissions:

```yaml
permissions:
  contents: read
  id-token: write        # OIDC token for Marshal; no stored secret
  pull-requests: write   # only for the PR comment
```

A complete workflow is in [`examples/github-workflow.yml`](../examples/github-workflow.yml). The pattern is to record `proposed` on pull requests and `shipped` after the deploy step succeeds:

```yaml
- uses: marshal-ledger/marshal/collect@v1
  with:
    event: proposed
    artifacts: .
    target: prod
    url: https://marshal.acme.example
```

| Input | Required | Meaning |
|---|---|---|
| `event` | yes | `proposed` or `shipped` |
| `artifacts` | yes | Path of the agent artifact, such as a bundle directory |
| `url` | yes | Base URL of the ingest API |
| `target` | no | Deployment target whose overrides to apply |
| `environment` | no | Environment the event applies to; defaults to `target` |
| `audience` | no | OIDC audience (default `marshal`) |
| `comment` | no | Comment on pull requests (default `true`) |
| `portability` | no | Space-separated platforms to score portability to in the comment, such as `bedrock copilot-studio` |
| `python-version` | no | Python used to run Marshal (default `3.12`) |

The report is also added to the job summary and exposed as the `report` output.

A typical comment:

```
**finance/invoice_agent** (databricks) · v14 → v15 · 3 changes, 2 high risk

HIGH  + tool tool/databricks.uc.function.main.finance.update_po (scope write, reversible unknown)
HIGH  + grant store/databricks.postgres.main.invoices CAN_CONNECT_AND_CREATE (write)
MED   - knowledge knowledge/databricks.uc.table.main.finance.vendor_faq_2023 (scope read)
Completeness 82%
```

Pull requests from forks don't receive OIDC tokens, so the action can't record events for them.
