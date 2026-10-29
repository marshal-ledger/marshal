---
title: "Run an AI agent inventory across dev, test and prod"
description: >-
  Where the Marshal server runs, which pipelines record which events for each environment, who owns what, and the
  order to roll it out in, for organizations with dev, test and production environments.
---

# Run an AI agent inventory across dev, test and prod

Most organizations deploy agents through several environments, such as dev, a test or staging copy of production, and prod. This guide covers where Marshal runs, which pipelines record to it, and who owns each part.

## One server for every environment

Run one Marshal server, in the same place as your other production tooling, such as CI/CD and single sign-on. Don't run a Marshal per environment. Environments are names on the events Marshal records, so one ledger shows that an agent is at version 7 in dev, 6 in test and 5 in prod, and can compare any two.

The server needs to be reachable from your CI runners, which push to it. It never connects to an agent platform, so it needs no route to Copilot Studio, Bedrock or any other platform. If you want to try Marshal upgrades before rolling them out, a second server with throwaway data is fine; it isn't one of your agents' environments.

Values that differ between environments, such as workspace hosts, warehouse IDs and deployed names, are kept apart from the agent's definition (see [the ledger model](../ledger-model.md)). The same definition promoted from dev to prod therefore keeps its version, and you can follow it from one environment to the next.

## Which pipelines record what

| Pipeline | Event | Environment | What it gives you |
|---|---|---|---|
| Every pull request that changes an agent | `proposed` | prod | A comment listing what changes compared with prod, ranked by risk, before the merge. See [Review agent changes in pull requests](pull-request-review.md). |
| The deploy job to prod | `shipped` | prod | What is live. This is the record an audit relies on. |
| The deploy job to test | `shipped` | test | What is waiting to be promoted, and how it differs from prod. |
| The deploy job to dev | `shipped` | dev | Optional. Dev changes often; record it only if teams want that history. |
| A scheduled export of what is live, which you run | `observed` | prod, and test if it's useful | Drift: an agent edited in a platform's web designer after it shipped. See [Detect drift](drift-detection.md). |

Use the same environment names in every pipeline. The `environment` input of the [GitHub Action](../github-action.md) and `MARSHAL_ENVIRONMENT` in the [GitLab CI template](../gitlab-ci.md) default to the deployment `target`, so set the environment explicitly where a pipeline has no target.

In GitHub Actions, the deploy jobs record a `shipped` event after their deploy step succeeds:

```yaml
  deploy-test:
    runs-on: ubuntu-latest
    environment: test
    steps:
      - uses: actions/checkout@v4
      # ... deploy to test ...
      - uses: marshal-ledger/marshal/collect@v0
        with:
          event: shipped
          artifacts: .
          environment: test
          url: https://marshal.acme.example

  deploy-prod:
    needs: deploy-test
    runs-on: ubuntu-latest
    environment: prod        # a GitHub environment with required reviewers
    steps:
      - uses: actions/checkout@v4
      # ... deploy to prod ...
      - uses: marshal-ledger/marshal/collect@v0
        with:
          event: shipped
          artifacts: .
          environment: prod
          url: https://marshal.acme.example
```

The scheduled export is only needed for platforms where people can edit a live agent outside the pipeline, such as Copilot Studio and Agentforce. The [ready-made workflows](https://github.com/marshal-ledger/marshal/tree/main/examples/scheduled) for both run in your CI with your own sign-in to the platform; Marshal only receives what they push.

Agents that no pipeline deploys can still be recorded: write a [Marshal file](../formats/marshal-file.md) by hand, which is recorded as stated by a person rather than read from a platform, or cover them with a scheduled export.

## Who may record what is live

A `proposed` event can come from any allowed repository. A `shipped` or `observed` event claims what is live, so the server accepts one only from callers that its `policy.shipped` lists: the main branch, a deployment environment with required reviewers, or a protected branch or environment (see [the ingest API](../ingest-server.md#configure)).

The top-level rule applies to every environment. To hold prod to a stricter rule, give it its own entry under `by_environment`. An event for an environment listed there must satisfy that entry, and the top-level rule no longer counts:

```yaml
policy:
  shipped:
    refs:
      - refs/heads/main            # dev and test: any deploy from main
    by_environment:
      prod:
        environments:
          - prod                   # only the GitHub environment named prod, with required reviewers
        protected_environments: true   # or a GitLab protected environment
```

Here a job on the main branch may record a deployment to dev or test, but a deployment to prod only from a job that ran in the approved prod environment. An entry's `refs` default to none, so `refs/heads/main` doesn't carry over to prod. Jenkins and Azure DevOps callers use `workflows` the same way: list the release job or the prod service connection under `prod`. The keys under `by_environment` are the ledger's environment names, the Action's `environment` input or `MARSHAL_ENVIRONMENT` in the GitLab template; the entries' `environments` are the CI provider's own environment names, which needn't be the same.

## Who owns what

| Team | Owns |
|---|---|
| Platform | The Marshal server and its database, the shared CI step (the Action or the GitLab template), the `policy.shipped` list, and upgrades. Run the same Marshal release on the server and in every pipeline, and upgrade them together (see [Upgrading](../deployment.md#upgrading)). |
| Each agent team | Adding the Marshal step to its pull request and deploy pipelines, reviewing the comments on its changes, and resolving drift on its agents. An agent belongs to the repository that first records it (see [Agent ownership](../ingest-server.md#agent-ownership)). |
| Security and governance | Reading the inventory and the pull request comments, and deciding which changes need their approval. |

Marshal ranks changes; it doesn't block merges. If a HIGH change should need a security reviewer, make that a rule in your repository: for example, require their review on agent definition paths with a `CODEOWNERS` file, or add a required check that reads the Action's `report` output and fails when it lists a HIGH change.

## Roll it out in steps

1. **Pull request comments and prod deployments, for one platform.** This alone gives you an inventory of what is in prod and a review of every change to it.
2. **Drift in prod**, with a scheduled export for each platform where agents can be edited live.
3. **The test environment, then the other platforms**, one pipeline at a time.
4. **Dev**, if teams ask for it.

Each step works without the next one. A record that only a manual import confirmed is shown as stale, so you can see which agents still need a pipeline.
