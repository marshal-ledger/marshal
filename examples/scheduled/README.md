# Scheduled observation workflows

A deploy pipeline tells Marshal what was **shipped**. These workflows tell it what is **live**: on a schedule, they export each agent's current definition with the platform's own CLI and push it as an `observed` event. Marshal compares the observation with what was last shipped to the same environment. A difference is drift, such as an edit made in a platform's web designer, and is reported with the same risk ranking as a proposed change.

| Workflow | Exports with | Signs in to the platform with |
|---|---|---|
| [`copilot-studio.yml`](copilot-studio.yml) | `pac solution export` | GitHub OIDC, through a federated credential on an Entra ID app registration. No stored secret. |
| [`agentforce.yml`](agentforce.yml) | `sf project retrieve start` | A connected app's JWT bearer flow. The private key is a repository secret. |

They are optional. Without them Marshal still records every change your pipelines ship; it can't detect drift, and agents whose only record came from a manual import are reported as stale.

## How they fit

- **They run in your infrastructure.** Each workflow uses credentials you already manage for that platform, and Marshal never receives them. Marshal itself never connects to an agent platform: it only accepts pushes. See [Network posture](../../docs/design.md#network-posture).
- **They push like a pipeline does.** The [GitHub Action](../../docs/github-action.md) signs the push with the workflow's OIDC token, so the repository that holds the workflow must be on the ingest server's allowlist, and the server's shipped-event policy also applies to observed events. See [Ingest API](../../docs/ingest-server.md).
- **The server records the channel.** Observed events are recorded on the `scheduled` channel, and the environment's freshness is the age of the last observation. The default policy treats a scheduled observation older than 7 days as stale, so a nightly or weekly run keeps records current.
- **Use the same environment name** as the deploy pipeline (`prod` in the examples). Drift compares an observation with the last shipped version in that environment.

## Least privilege

Give each workflow an identity that can export definitions and nothing else, and keep it out of reach of pull requests. In GitHub, put the platform credentials in an environment with required reviewers or limit them to the default branch.

- **Copilot Studio:** add the app registration to the environment as an application user with a security role that can export solutions. Power Platform can't export a managed solution, so list the unmanaged solutions that hold your agents, usually in the environment where makers build them. Agents only in managed solutions need another source, such as the Dataverse Web API, which these examples don't cover.
- **Agentforce:** use a dedicated integration user pre-authorized for the connected app. Salesforce requires a metadata permission to retrieve metadata, so treat this user's key as sensitive and rotate it with your usual process.

## Other platforms

The pattern is the same everywhere: export with the platform's CLI or API, then run the GitHub Action with `event: observed`, or `marshal push <path> --event observed --environment <name>` from another CI system. [Platform exportability](../../docs/platform-exportability.md) lists what each platform can export.
