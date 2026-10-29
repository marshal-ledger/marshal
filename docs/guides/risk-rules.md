---
title: "Set your organization's AI agent risk rules"
description: >-
  Rate AI agent changes by your organization's own rules: approved models and providers, sensitive tools, hosts and
  knowledge sources, identity modes and platform fields. Set on the Marshal server only, so agent teams can't change them.
---

# Set your organization's AI agent risk rules

Marshal rates every change to an agent with [built-in rules](../ledger-model.md#risk-ranking): a new write tool is HIGH, a new model from the same provider is MED, a new description is LOW. Your organization knows more: which models are approved, which API moves money, which data must not be kept longer. Risk rules add that knowledge to the ratings in every pull request comment, API response and web view page.

Rules live in the [ingest server's](../ingest-server.md) configuration, under `policy.risk`. Only the server reads them. Pipelines send agent definitions, never ratings, and nothing in a definition or a request changes how it is rated, so an agent team can't lower the rating of its own change.

## How rules combine with the built-in ratings

- The built-in rules are the floor. Your rules adjust their ratings; they don't replace them.
- Raising a rating needs nothing more than the rule. A `reason` is optional and shown if given.
- Lowering a rating needs a `reason`, and the report shows it on that line. A rule that lowers without one stops the server from starting.
- A removed guardrail is never lowered. When instructions lose a line with a word such as "never" or "approval", the line keeps its built-in HIGH whatever the rules say, because the word list can't tell a reworded limit from a dropped one.
- When rules disagree, a raise wins over a lower. Among rules that set the same level, the first in the file is the one named.
- Every line a rule changed names the rule, the built-in rating and the reason. Lines no rule changed look as they always have.
- A rule that matches nothing is fine. A misspelled key, an unknown category or identity, or a target that isn't one is an error when the server loads its configuration.

## An example

The rules in [`examples/marshal-server.yaml`](../../examples/marshal-server.yaml):

```yaml
policy:
  risk:
    approved_models:
      providers: [anthropic, amazon]
      models: ["anthropic.claude-*", "claude-*", "amazon.nova-*"]
      raise_to: HIGH        # a change to any other model or provider

    rules:
      - name: payments-api
        match:
          targets: ["host:payments.acme.example"]
        raise_to: HIGH
        reason: moves money

      - name: shared-credential-tools
        match:
          identity: [shared_credential]
          categories: [tools]
          kinds: [added]
        raise_to: HIGH
        reason: every user acts with the author's access

      - name: conversation-retention
        match:
          paths: ["extensions.bedrock.idle_session_ttl", "extensions.bedrock-agentcore.memory*"]
        raise_to: MED
        reason: changes how long conversations are kept

      - name: approved-model-upgrades
        match:
          paths: ["model.requested", "model.resolved"]
        lower_to: LOW
        reason: switching between approved models is routine here
```

A pull request against the public Bedrock HR assistant adds `dynamodb:UpdateItem` to its role, moves it from Titan to Nova Pro, and keeps idle sessions for an hour instead of 15 minutes. With these rules, the comment reads:

<!-- example: pr-comment-org-rules -->
```
**cfn/hr-resources/HRAgent** (bedrock) · v1 → v2 · 3 changes, 1 high risk

HIGH  + grant other/aws.* dynamodb:UpdateItem (write)
MED   ~ extensions.bedrock.idle_session_ttl: 900 → 3600  [raised from LOW by org rule conversation-retention: changes how long conversations are kept]
LOW   ~ model.requested: amazon.titan-text-premier-v1:0 → amazon.nova-pro-v1:0  [lowered from MED by org rule approved-model-upgrades: switching between approved models is routine here]
Completeness 73%
```

Had the model moved to one not on the list, `approved_models` would have raised the line to HIGH, overruling the lower.

## Approved models

`approved_models` lists the providers and models your organization approves, as globs in which `*` matches anything. Case is ignored.

| Key | Meaning |
|---|---|
| `providers` | Globs over the model provider, such as `anthropic` or `amazon` |
| `models` | Globs over the requested and resolved model, such as `claude-*` or `anthropic.claude-*` |
| `raise_to` | The rating for a change to anything not on the list: `HIGH` (default) or `MED` |

A change of provider to one not listed, or of the requested or resolved model to one not listed, is raised and named `approved_models`, with a reason such as "gpt-5 is not an approved model". A model that becomes unknown isn't on any list, so it is raised too. Give `providers`, `models` or both; a list left out isn't checked.

Rules rate changes. An agent already shipped with an unapproved model shows nothing until its model changes; to find those, search the inventory with `marshal ls --provider`.

## Rules

Each entry in `rules` has a `name`, a `match`, and either `raise_to` or `lower_to`.

| Key | Meaning |
|---|---|
| `name` | Shown on every line the rule changes. Letters, digits, `-`, `_` and `.`; unique |
| `match` | Which changes the rule applies to. Every condition given must hold; within a list, any entry may |
| `raise_to` | Matching changes are rated at least this: `MED` or `HIGH` |
| `lower_to` | Matching changes are rated at most this: `LOW` or `MED`. Needs `reason` |
| `reason` | Why, in a phrase. Shown beside the rating. Required with `lower_to` |

The conditions in `match`:

| Condition | Matches |
|---|---|
| `categories` | The change's category: `permissions`, `tools`, `model`, `knowledge`, `instructions`, `metadata` or `extensions` |
| `kinds` | `added`, `removed` or `changed` |
| `paths` | Globs over the change's path, as reports print it: `model.provider`, `permissions.identity`, `description`, `extensions.copilot-studio.*.connection_mode` |
| `refs` | Globs over the ref of the tool, knowledge source or grant target the change is about, such as `tool/http.*` or `knowledge/databricks.uc.table.main.hr.*` |
| `targets` | What that ref reaches, as [entity resolution](../ledger-model.md#resolving-refs-across-platforms) derives it: `host:payments.acme.example`, `host:*.acme.example`, `mcp:npm:@notionhq/notion-mcp-server`, `http:POST https://api.acme.example/refunds/{}`, `url:…`, `openapi:…`, `lambda:…` |
| `identity` | The agent's identity before or after the change: `service_principal`, `on_behalf_of_user`, `shared_credential` or `mixed` |

A `targets` rule applies whenever a tool, knowledge source or grant that reaches the target is added, removed or changed, on any platform: a Copilot Studio HTTP request step, an MCP server URL in an ADK config and an ADL network permission for `payments.acme.example` all match `host:payments.acme.example`. Targets come from what the definition states and from documented equivalents. A link an agent team states in a [Marshal file](../formats/marshal-file.md) can make a raising rule match, but never a lowering one.

## Try rules before the server applies them

`marshal diff`, `marshal collect` and `marshal web` take `--risk-rules FILE`, which reads a server configuration file or a file holding only the `policy.risk` section. Without the flag they use the built-in ratings.

```console
$ marshal diff cfn/hr-resources/HRAgent --risk-rules marshal-server.yaml
```

The flag is for trying rules. `marshal push` has no such flag: what a pipeline sees in its comment is what the server decided.

## Rolling rules out

Rules change ratings from the next report on; nothing stored changes, because ratings are computed when a report or page is made. Restart the server after editing them. Start with raises, which can only add attention, and add lowers when reviewers agree a kind of change is routine. Keep the configuration file in a repository the platform team owns, reviewed like any other policy.

Clients before 0.10.0 can't read a report line a rule changed. Upgrade `marshal push` and the server together, as [Upgrading](../deployment.md#upgrading) describes.
