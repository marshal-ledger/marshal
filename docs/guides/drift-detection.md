---
title: "Detect drift between shipped and live AI agents"
---

# Detect drift between shipped and live AI agents

Agents built in Copilot Studio or Agentforce can be edited in the platform's web designer after they ship. The change never passes through a pull request, so nobody reviews it. Marshal compares what is live with what your pipeline last shipped, and reports any difference with the same risk ranking as a proposed change.

## How it works

Three events describe an agent in an environment:

| Event | Recorded by | Means |
|---|---|---|
| `proposed` | a pull request | what someone wants to ship |
| `shipped` | the deploy pipeline | what was deployed |
| `observed` | a scheduled export | what is live now |

When an observation differs from the last shipped version in the same environment, the environment is in drift.

## See it locally

Record a Copilot Studio agent as shipped, then an export in which someone switched one of its tools from each user's credentials to the maker's:

```console
$ marshal collect mcp-agent --event shipped --environment prod
$ marshal collect live-export --event observed --environment prod --channel scheduled
observed prod: eval_testAgent v2 (new version)

eval_testAgent (copilot-studio)  prod: v1 → v2
  DRIFT: the live definition differs from shipped v1

  HIGH  ~ permissions.identity: mixed → shared_credential
  HIGH  ~ extensions.copilot-studio.actions.SearchDocs.connection_mode: Invoker → Maker
  HIGH  ~ extensions.copilot-studio.actions.SearchDocs.credentials: end user → maker
```

The tool now runs every user's request with its author's credentials. That is exactly the kind of change that should have been reviewed.

```console
$ marshal status eval_testAgent
prod: shipped v1, observed v2, confirmed 2026-10-27 by scheduled (0d ago), DRIFT: live differs from shipped
$ marshal status eval_testAgent --check   # exits 1 on drift or a stale record, for alerts
```

The same agent is in drift in the [live demo](https://marshal-ledger.github.io/marshal/demo/).

## Run the exports on a schedule

The exports run in your infrastructure, with credentials you already manage for each platform. Marshal never receives them and never connects to the platform; it only accepts the push.

- **Copilot Studio:** [`copilot-studio.yml`](https://github.com/marshal-ledger/marshal/blob/main/examples/scheduled/copilot-studio.yml) exports each solution nightly with `pac solution export`, signing in to Power Platform with a federated credential, so no secret is stored.
- **Agentforce:** [`agentforce.yml`](https://github.com/marshal-ledger/marshal/blob/main/examples/scheduled/agentforce.yml) retrieves the agent metadata with `sf project retrieve start`.
- **Other platforms:** export with the platform's CLI or API, then run `marshal push <path> --event observed --environment prod`. [Platform exportability](../platform-exportability.md) lists what each platform can export.

Use the same environment name as the deploy pipeline. An observation older than 7 days marks the environment stale, so a nightly or weekly run keeps every record current. See [Scheduled observation workflows](https://github.com/marshal-ledger/marshal/tree/main/examples/scheduled) for least-privilege setup.
