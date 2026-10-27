# Security review

This document is for the security, architecture and data-classification reviewers who approve a Marshal deployment. It covers what Marshal stores and where, what it never stores, how callers are authenticated, what it connects to, how releases are built and signed, and what Marshal doesn't do. Each statement links to the code or configuration that implements it. Where Marshal has no control for something, this document says so.

To report a vulnerability, see [SECURITY.md](../SECURITY.md).

## Summary

| Question | Answer |
|---|---|
| What runs | One service: the ingest API and a read-only web view, as one container ([`server.py`](../src/marshal_ledger/server.py), [`web.py`](../src/marshal_ledger/web.py)) |
| Where data lives | Your SQLite file or Postgres database. Nothing is sent to the Marshal project or any third party |
| What data | Agent definitions: names, descriptions, model, instructions, tools, knowledge sources, grants, access rules, and who recorded each version |
| Credentials stored | None by design. Readers drop each format's credential fields, record environment variables by name, and redact credential patterns in free text (limits below). CI authenticates with short-lived OIDC tokens, which are not stored |
| Who can write | Pipelines with a valid OIDC token from a configured issuer (GitHub Actions, GitLab CI, Jenkins or Azure DevOps), from an allowlisted repository, Jenkins job or Azure DevOps pipeline |
| Who can read | Anyone the SSO proxy lets through. There are no per-agent read permissions |
| Outbound connections | The store, and each configured OIDC issuer's signing keys. Never an agent platform |
| Retention and deletion | None built in. The ledger is append-only, and there is no delete command |
| Audit trail | Every event records the issuer, repository, ref, workflow, commit and pipeline run from the token. Reads are not logged by Marshal |
| Releases | Signed multi-arch image with SBOM and SLSA provenance; PyPI packages published with trusted publishing |

## Architecture

```
CI runner ──(OIDC token)──▶ POST /v0/events ─┐
                                               ├─ Marshal service ──▶ store (SQLite or Postgres)
user ──▶ SSO proxy ──(user header)──▶ /, /v0/ ┘        │
                                                         └──▶ OIDC issuer's signing keys
```

- **Readers** run in the pipeline, inside the `marshal` command line or the [GitHub Action](github-action.md). They parse agent definitions in the repository (or a platform export the organization made) into ledger records.
- **The ingest API** verifies the pipeline's token, re-normalizes each record and writes it to the store. It is the only component that holds store credentials.
- **The web view** shows the ledger read-only, behind the organization's SSO proxy.

The service listens on plain HTTP, port 8080. TLS terminates at the ingress, OpenShift Route or proxy in front of it.

## What is stored, and where

Everything is in the store you configure: a SQLite file or a Postgres database ([`store.py`](../src/marshal_ledger/store.py)). Its location comes from the server configuration, usually through an environment variable from a Kubernetes Secret, so the database password isn't in the configuration file. Marshal doesn't encrypt the store itself; use your database's or volume's encryption at rest. For TLS to Postgres, add `sslmode=verify-full` to the store URL. On Azure Database for PostgreSQL, the server can sign in with a Microsoft Entra ID token for its managed identity instead of a password (`store_auth: azure`, [Running Marshal on Azure](azure.md)), so the store URL holds no password.

Each request opens its own database connection. If the database can't be reached, the ingest API and the web view answer `503` without naming the database host. A write that loses its connection is rolled back by the database and never retried, except that a connection lost while committing leaves the outcome unknown; reads are retried once on a new connection.

| Table | Contents |
|---|---|
| `agents` | Agent id, platform, display name, first and last seen |
| `versions` | Each distinct version of an agent: the full normalized record as JSON (below) |
| `events` | Each proposed, shipped or observed event: environment, time, commit, pipeline run, artifact path, environment-bound values, the caller's identity, and the channel |
| `version_refs` | Which tools, knowledge sources and grant targets each version uses |
| `entities` | Shared tools, knowledge sources and grant targets, with display names |
| `agent_owners` | The issuer and repository each agent is bound to |
| `meta` | The schema version |

A version record ([`model.py`](../src/marshal_ledger/model.py)) holds:

- display name, description, model and its parameters;
- **instructions**: the system prompt text and its SHA-256 hash, or only the hash in hash-only mode;
- tools, knowledge sources and permission grants, as references such as `tool/databricks.uc.function.main.finance.update_po`;
- **access rules**: principals allowed to use or manage the agent, as the definition names them. These can be user names, email addresses or group names, which may be personal data;
- platform-specific fields under `extensions`, so a definition can be written back in its native format;
- the source format, artifact path and position within it.

Environment-bound values (`bindings`) such as warehouse IDs, endpoint URLs and environment variable *names* are stored with each event, not with the version.

**Classification guidance.** A ledger is a map of what each agent can do and which systems it touches, and it contains the agents' instructions unless hash-only mode is on. Classify it at least as high as the repositories its definitions come from.

**Logs.** The server writes Uvicorn's access log to standard output: the client address, the request line (method, path and query string) and the status code. It doesn't log request bodies or headers, so neither tokens nor the user header appear. Behind an ingress, the client address is the ingress's address.

## What is never stored, and how

**Credential values.** Each reader decides field by field what to copy. Fields the ledger model has, such as the model, tools and grants, are mapped into the record. For formats that Marshal can write back, the reader keeps the format's other fields under `extensions`, minus the fields that carry credentials in that format: environment variable values, headers, authentication configurations and key files. For example, [`readers/agentspec.py`](../src/marshal_ledger/readers/agentspec.py) drops `api_key`, `headers`, `auth`, `env` and key files, [`readers/oasf.py`](../src/marshal_ledger/readers/oasf.py) drops environment variable defaults and connection headers, and [`readers/marshalfile.py`](../src/marshal_ledger/readers/marshalfile.py) keeps environment variables by name and warns. Tests check that secret-like placeholders in fixtures don't reach the record, for example in [`test_open_formats.py`](../tests/unit/test_open_formats.py).

**Credentials pasted into text.** Free text that is copied (instructions, descriptions, display names) passes through [`redact`](../src/marshal_ledger/redact.py), which replaces Databricks, AWS, GitHub, Slack, OpenAI- and Anthropic-style and Google API keys, private key blocks and JWTs with `[REDACTED]`. Readers apply it as they read, and the ingest API applies it again on receipt ([`service.normalize`](../src/marshal_ledger/service.py)), so a store holds the same result whichever client sent the record.

**Instruction text, if you choose.** With `hash_only: true` in the server configuration, the server drops instruction text and keeps only its hash, whatever clients send. Diffs then report that instructions changed without showing them. The hash is unsalted SHA-256, so someone who can guess the exact text can confirm the guess.

**Tokens.** The client reads the CI token from the environment at run time and never prints or writes it ([`client.py`](../src/marshal_ledger/client.py)). The server keeps only the token's identity claims (issuer, repository, ref, workflow, commit, run), never the token. With `store_auth: azure`, the database access token is held in memory, reused until five minutes before it expires, and never logged or stored ([`dbauth.py`](../src/marshal_ledger/dbauth.py)).

**Limits of these controls.**

- Redaction matches known credential formats. It doesn't catch a password written as plain prose.
- Credential fields are removed by name, per format. A credential in a field the reader doesn't know about, in a format whose other fields are kept under `extensions` (ADL, for example), is stored as written: redaction covers free text, not `extensions`. The [format guides](formats/) list what each reader keeps.
- The allowlists live in the readers, which run in the pipeline. The ingest API validates every record against the schema and rejects unknown fields, and re-runs redaction on free text, but stores other fields (such as `extensions` and `bindings`) as an authenticated pipeline sends them. A pipeline that builds its own records instead of using the readers can put any schema-valid value there. Limit write access accordingly (below).

## Authentication and authorization

### Pipelines: the ingest API

Pipelines authenticate with their CI provider's OIDC token; no shared secret is stored in CI ([`oidc.py`](../src/marshal_ledger/oidc.py), [ingest API guide](ingest-server.md), [Jenkins and Azure DevOps](other-ci.md)). Supported issuers are GitHub Actions, GitLab CI, Jenkins with the OpenID Connect Provider plugin, and Azure DevOps. For each request the server checks:

| Check | Failure |
|---|---|
| A bearer token signed by a configured issuer (RS or ES algorithms only), with the configured audience, unexpired (30 seconds leeway), with `exp`, `iat`, `iss` and `aud` present, naming its caller (the repository, or `sub` for Jenkins and Azure DevOps) | `401` |
| The token's caller matches one of the issuer's `repositories` patterns: a repository, a Jenkins job URL or an Azure DevOps subject. An issuer such as GitHub signs tokens for every repository it hosts, a Jenkins controller for every job, and an Azure DevOps organization for every pipeline, so this allowlist is what limits writes | `403` |
| For `shipped` and `observed` events, the token's ref, deployment environment or workflow satisfies `policy.shipped`, such as `refs/heads/main`, a GitHub environment with required reviewers, or a listed Jenkins job or Azure DevOps subject | `403` |
| Each agent is unbound, or bound to the same issuer and repository. An agent is bound to the first repository that records it | `403` |
| The body is a valid event of at most 500 records | `422` |

The commit and pipeline run come from the token when it carries them, rather than from the request body. Azure DevOps tokens carry neither, and Jenkins tokens carry only the build number unless an administrator adds a `sha` claim template, so for those the commit (and, for Azure DevOps, the run) is what the pipeline sent. Version hashes and quality scores are recomputed by the server.

Azure DevOps tokens always have the audience `api://AzureADTokenExchange`, so a token sent to Marshal is also accepted by any system federated with the same subject, such as Microsoft Entra ID for a service connection. Whoever runs the server could use it until it expires. [Jenkins and Azure DevOps](other-ci.md#audience-and-reuse) recommends pipeline tokens and subjects federated with nothing else.

Because an agent is bound to the first repository that records it, any allowlisted repository can claim an agent id that hasn't been recorded yet. Keep `repositories` as narrow as your organization allows. Moving an agent to another repository is an administrative operation through the store API (`Store.transfer`); there is no HTTP endpoint for it.

### People: the web view

The web view and its JSON API under `/v0/` delegate sign-in to the organization's SSO proxy (for example oauth2-proxy or a corporate access gateway). Every request must carry the configured user header (default `X-Forwarded-Email`), and requests without it get `401` ([`web.py`](../src/marshal_ledger/web.py)). Marshal shows the header's value on the page; it doesn't check it against anything.

That has two consequences:

- **Every signed-in user can read every agent.** There are no roles or per-agent read permissions. Restrict who the proxy admits.
- **The proxy must be the only route to the web view.** Anything that reaches the pod directly can set the header itself and read the ledger. The proxy must overwrite any user header the client sent. The ingress that runners use should route only `POST /v0/events`, not `/`.

The Helm chart can add a NetworkPolicy that admits traffic only from the peers you name, such as the ingress controller and the proxy ([deployment guide](deployment.md#network-policy)):

```yaml
networkPolicy:
  enabled: true
  from:
    - namespaceSelector:
        matchLabels: {kubernetes.io/metadata.name: ingress-nginx}
    - namespaceSelector:
        matchLabels: {kubernetes.io/metadata.name: sso-proxy}
```

If you don't run an SSO proxy, turn the web view off (`web.enabled: false`) and read the ledger with the command line against the store.

### Unauthenticated endpoints

| Path | Serves |
|---|---|
| `/healthz` | `{"status": "ok"}`, for probes |
| `/favicon.svg` | The logo |

FastAPI's generated API pages (`/docs`, `/redoc`, `/openapi.json`) are turned off; the API is described in the [ingest API guide](ingest-server.md).

## Network posture

Marshal never starts a connection to an agent platform ([design](design.md#network-posture)). Definitions reach it as files that readers parse in a pipeline, and as pushes to the ingest API.

| Direction | Peer | Why |
|---|---|---|
| Inbound | CI runners | `POST /v0/events` |
| Inbound | SSO proxy | Web view and `/v0/` JSON |
| Outbound | Each configured OIDC issuer, such as `token.actions.githubusercontent.com`, a GitLab instance, a Jenkins controller (`<jenkins>/oidc`) or `vstoken.dev.azure.com` | `<issuer>/.well-known/openid-configuration`, then its key set, cached for an hour; a token with an unknown key id causes a new fetch at most once a minute. Set `jwks_uri` to fetch the keys from a mirror and skip discovery. Fetches honor `HTTPS_PROXY`, `HTTP_PROXY` and `NO_PROXY`, and trust OpenSSL's default CA store, which `SSL_CERT_FILE` and `SSL_CERT_DIR` replace ([details](ingest-server.md#signing-keys-and-egress)) |
| Outbound | The store | SQLite on a volume, or Postgres |
| Outbound | Microsoft Entra ID, only with `store_auth: azure` | A database access token from the managed identity endpoint or Entra ID's token endpoint, when the store connects and the cached token is near expiry |

That is all the server connects to. The web view's pages load no external fonts, scripts or images (a test checks this). FastAPI includes the OpenTelemetry API, which does nothing unless an OpenTelemetry SDK and exporter are installed and configured; the image contains neither. The pipeline side connects to the ingest URL, the CI provider's token endpoint and, for pull request comments, the GitHub API.

`marshal demo` downloads public agent definitions from GitHub to build a demonstration store. It is a command-line convenience; the server never runs it.

## Container and Kubernetes

The image ([`Dockerfile`](../Dockerfile)) is based on `python:3.12-slim` and runs as UID 10001 in group 0, or as any UID OpenShift assigns. It writes nothing outside its volumes, so it runs with a read-only root filesystem; a SQLite store goes on a volume at `/var/lib/marshal`. The Helm chart's defaults: non-root, a read-only root filesystem, no privilege escalation, all capabilities dropped, the `RuntimeDefault` seccomp profile, the store URL from a Secret. On OpenShift, it fits the `restricted-v2` SCC with the UID and fsGroup left to the SCC ([deployment guide](deployment.md#openshift)).

## Supply chain

**Releases** ([`release.yml`](../.github/workflows/release.yml)), built only from a version tag on `main`, after the full test suite passes on that commit:

- **Container image**, for `linux/amd64` and `linux/arm64`, with an SPDX SBOM and SLSA provenance (`mode=max`) attached by BuildKit, and signed keylessly with Sigstore cosign by the release workflow's identity. [Verify and mirror](deployment.md#verify-and-mirror) shows the `cosign verify` command and how to copy the image with its signature. The signature covers the image index, which lists the SBOM and provenance manifests.
- **PyPI packages**, published with trusted publishing (no stored API token), carrying PyPI's attestations.
- **GitHub release**: the wheel, the sdist and the packaged Helm chart. These release assets aren't signed separately; the chart isn't signed.

**Repository controls:**

- Third-party Actions in workflows and in the collect Action are pinned to commit SHAs; a test fails on any that isn't ([`test_examples.py`](../tests/unit/test_examples.py)). Workflow `run:` scripts take values through `env:` rather than inline expressions, which the same test enforces against script injection.
- Dependabot opens weekly updates for the Actions, the Python dependencies and the container base image, and Dependabot security updates are on. Python minimum versions are kept low and tested by a CI job that installs the oldest allowed versions.
- CodeQL code scanning (GitHub's default setup) analyzes the Python code and the workflows.
- Secret scanning and push protection are on.
- Workflows get a read-only `GITHUB_TOKEN` unless a job asks for more.
- `main` changes only through pull requests that pass the required checks (tests on Python 3.10 to 3.13, chart, image, Action); force pushes and deletion are blocked. Required approvals are zero, since the project has one maintainer. Release tags (`v*.*.*`) can be created, moved or deleted only by repository administrators.

**Dependencies.** The client needs `pydantic` and `pyyaml`. The server adds `fastapi`, `uvicorn` and `pyjwt[crypto]`, `psycopg[binary]` for Postgres, and `azure-identity` for Entra ID sign-in to Azure Database for PostgreSQL; the image includes all of them. The image's SBOM lists every package with its version.

## Retention and deletion

Marshal has no retention controls. The ledger is append-only: versions and events are kept indefinitely, and there is no command or API to delete an agent, a version or an event, or to expire old ones.

If you must delete data, for example to honor a request about a person named in an access rule, do it in the database: an agent's rows are in `events`, `version_refs`, `versions`, `agent_owners` and `agents`, keyed by agent id. Marshal doesn't provide or test a procedure for this. Back up the database first, and remember that copies remain in database backups. Because records only accumulate, plan storage and backup retention in the database.

## Audit trail

Each event in the store records:

- the event (`proposed`, `shipped` or `observed`), environment and version;
- the time, set by the server;
- the caller's OIDC issuer, repository (for Jenkins the job URL, for Azure DevOps the subject, or their parent with `owner: parent`), ref and workflow, from the verified token;
- the commit and pipeline run, from the token where it carries them;
- the channel: `ci` for pipeline events, `scheduled` for observed events from a scheduled export, `manual` for local `marshal collect`.

`marshal log <agent> --db <store>` lists an agent's events with this identity, and the web view shows each agent's history.

What it doesn't record:

- **Reads.** Marshal doesn't log who viewed what beyond the access log's request lines, which don't include the user. Use the SSO proxy's logs.
- **Rejected writes.** A refused request appears in the access log as a `401`, `403` or `422`, without the caller's identity.
- **Changes made directly in the database.** Events aren't hash-chained or signed, so someone with write access to the database can alter history without Marshal noticing. Restrict database write access to the service account, and use the database's own audit logging if you need that assurance.

## Checklist for a deployment

- Keep each issuer's `repositories` to your organization's repositories, as narrowly as practical.
- Require a protected branch or a deployment environment with reviewers in `policy.shipped`. For Jenkins and Azure DevOps, list only the jobs or subjects that release in `policy.shipped.workflows`.
- Put the web view behind the SSO proxy only; expose only `POST /v0/events` to runners; enable the chart's NetworkPolicy. Turn the web view off if there is no proxy.
- Turn on `hash_only` if agent instructions are more sensitive than the rest of the definition.
- Use Postgres with TLS and encryption at rest; keep its credentials in a Secret, or on Azure sign in with a managed identity instead of a password.
- Verify the image signature at admission, or pin the digest you verified.
- Classify the store at the level of the source repositories, and plan backup retention, since Marshal deletes nothing.
