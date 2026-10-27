# Review agent permission changes in pull requests

An agent's power is spread across files: the model and instructions in one place, the tools in another, the IAM role or grants somewhere else. A reviewer looking at a diff of a CloudFormation template sees `dynamodb:UpdateItem` added to a policy; Marshal tells them that the HR agent can now write to DynamoDB, and ranks that first.

## What the comment looks like

Take the public Bedrock HR assistant template and add one action to its agent's role:

```yaml
- bedrock:Retrieve
- s3:GetObject
- dynamodb:UpdateItem   # added in this pull request
```

The pull request gets this comment:

<!-- example: pr-comment-dynamodb -->
```
**cfn/hr-resources/HRAgent** (bedrock) · v1 → v2 · 1 change, 1 high risk

HIGH  + grant other/aws.* dynamodb:UpdateItem (write)
Completeness 73%
```

Changes are ranked by what they let the agent do. Identity changes, new write or admin grants, new tools whose scope is write or unknown, a new model provider, and instructions that lose a line stating a limit (such as "never" or "approval") are HIGH. New read or execute grants, new execute tools, principal changes, other instruction changes, a new model from the same provider and guardrail settings are MED. Removals, read-only additions and descriptions are LOW. The full rules are in [Risk ranking](../ledger-model.md#risk-ranking).

## Set it up on GitHub

Add the [GitHub Action](../github-action.md) to the workflow that deploys the agent: `proposed` on pull requests, `shipped` after the deploy succeeds.

```yaml
permissions:
  contents: read
  id-token: write        # OIDC token for Marshal; no stored secret
  pull-requests: write   # the comment

steps:
  - uses: actions/checkout@v4
  - uses: marshal-ledger/marshal/collect@v0
    with:
      event: proposed
      artifacts: cfn
      environment: prod
      url: https://marshal.acme.example
```

The pipeline signs in to Marshal with its OIDC token, and the server accepts it only from repositories on its allowlist. See the [Ingest API](../ingest-server.md) for the server side, and [`examples/github-workflow.yml`](https://github.com/marshal-ledger/marshal/blob/main/examples/github-workflow.yml) for a complete workflow.

On GitLab, Jenkins or Azure DevOps, run `marshal push` with the same arguments, and post its `--report` file as a comment. See [Jenkins and Azure DevOps](../other-ci.md).

## Try it without a server

`marshal collect` writes to a local store and prints the same ranking:

```console
$ marshal collect path/to/main --event shipped --environment prod
$ marshal collect path/to/branch --event proposed --environment prod --report comment.md
```

It works the same way for [Databricks bundles](../formats/databricks.md) (grants on Unity Catalog functions, tables and serving endpoints), [Copilot Studio](../formats/copilot-studio.md) (connection modes and connector operations), [Agentforce](../formats/agentforce.md) and [agent code](../formats/python.md).
