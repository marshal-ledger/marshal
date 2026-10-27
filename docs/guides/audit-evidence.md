# An AI agent inventory for risk and audit reviews

Model risk, internal audit, security and AI governance reviews ask the same questions about every agent: what it is, what model it uses, what it can read and change, whose authority it acts with, who changed it and when, and whether what is running is what was approved. Marshal answers them from the agents' own definitions, across platforms, with a record of where each answer came from.

Marshal supplies evidence. It doesn't make an organization compliant with any regulation or framework, and its records are only as complete as the definitions it is given.

## The questions, and where the answers come from

| Question | Command | Field |
|---|---|---|
| Which agents exist, on which platforms? | `marshal ls` | agent, platform |
| What model does each use? | `marshal ls --provider anthropic`, `marshal read` | `model.requested`, `model.provider` |
| What can it change? | `marshal read`, `marshal deps` | `tools[].scope`, `permissions.grants` |
| Whose authority does it act with? | `marshal ls --identity on_behalf_of_user` | `permissions.identity`, `principal` |
| Who may use it? | `marshal read` | `permissions.access` |
| What changed, and how risky was it? | `marshal diff`, `marshal log` | ranked changes per version |
| Is what runs what was shipped? | `marshal status --check` | drift and freshness per environment |
| Which agents does a system's outage or breach affect? | `marshal deps "<system>*"` | dependents of a tool, knowledge source or grant |

## Provenance: what is known, and how

Every value is one of:

- **declared:** the artifact states it, with the file and line;
- **derived:** a rule computed it, and the rule is the basis, for example a tool's scope from the HTTP method or IAM action verb;
- **attested:** a person wrote it in a [Marshal file](../formats/marshal-file.md), for agents no reader covers, such as ones on an in-house platform;
- **unknown:** the artifact doesn't say, with the reason.

Marshal never guesses. An agent whose identity can't be determined is `unknown`, not assumed safe, and its completeness score shows how much of the record is known. Low completeness is itself a finding.

## History

Each version is identified by a hash of its definition. Every event records when it happened, the environment and the channel: `ci` for a push from a pipeline, with its repository, workflow and commit; `scheduled` for an export; `manual` for a local import such as this one:

```console
$ marshal log cfn/hr-resources/HRAgent
2026-10-27 15:11:04Z  shipped   prod          v1  manual
2026-10-27 15:11:04Z  proposed  prod          v2  manual
```

The ingest API accepts pushes only from pipelines whose OIDC token it verifies, from repositories on its allowlist, so a record of a shipped event can be traced to the pipeline that shipped it. See [Audit trail](../security-review.md#audit-trail).

## Exporting the evidence

`marshal ls --output json` and the web view's read-only JSON API (`/v0/agents`, `/v0/agent`, `/v0/deps`) give the inventory to a GRC tool or a spreadsheet. `marshal read --hash-only` records instructions as a hash, for ledgers that must not hold prompt text.

For the reviewers of Marshal itself, the [security review](../security-review.md) covers what is stored, authentication, network posture, supply chain, retention and the audit trail.
