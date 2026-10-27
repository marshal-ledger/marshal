---
title: "Vertex AI Agent Engine agent inventory"
description: >-
  How Marshal reads saved Vertex AI Agent Engine API responses to inventory deployed agents, without connecting to Google Cloud.
---

# Vertex AI Agent Engine

Format id: `vertex-agent-engine`. Reads saved responses of the Agent Engine API (`reasoningEngines.get` or `reasoningEngines.list`, JSON) at inventory tier.

Marshal never calls the API. A job the organization runs where it already has access saves the response, for example:

```console
$ gcloud ai reasoning-engines list --region=us-central1 --format=json > engines.json
```

The job then commits the file, or pushes it with `marshal push engines.json --event observed`. See [Network posture](../design.md#network-posture).

| Ledger field | Source | Provenance |
|---|---|---|
| agent id | `vertex-agent-engine/<display name slug>` | |
| `display_name`, `description` | `displayName`, `description` | declared |
| `permissions.identity` | `identityType: AGENT_IDENTITY` → the agent's own identity; otherwise the service account it runs as | declared / derived |
| `permissions.principal` | `serviceAccount`; without one, the project's Reasoning Engine Service Agent | declared / derived |
| `model`, `instructions`, `tools` | Unknown: set in the agent's code | unknown |
| `extensions.vertex-agent-engine` | Framework, class methods, environment variable names, secret references (secret name only), deploy source (Developer Connect repository, entrypoint, container image), resource limits, labels | declared |
| `bindings` | Resource name, package locations, create and update times | |

Environment variable values are never copied. To fill in the model, instructions and tools, read the agent's source with the [Python reader](python.md); the deploy source in the extension says where it lives.
