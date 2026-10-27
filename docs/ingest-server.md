# Ingest API

The ingest API is how pipelines write to a shared Marshal store. It authenticates each request with the CI provider's OIDC token, applies the ingestion policy, re-normalizes the records and writes them. It is the only component that holds store credentials. For the design rationale, see [Ingestion and authentication](design.md#ingestion-and-authentication).

## Run

```console
$ pip install 'marshal-ledger[server,postgres]'
$ marshal serve --config marshal-server.yaml --host 0.0.0.0 --port 8080
```

Run it behind the organization's ingress or load balancer, which terminates TLS and enforces a request size limit. Only `POST /v0/events` and `GET /healthz` need to be reachable from CI runners.

## Configure

See [`examples/marshal-server.yaml`](../examples/marshal-server.yaml).

| Key | Meaning |
|---|---|
| `store` | A SQLite file path, a `postgresql://` URL, or `env:VARIABLE` to read either from the environment |
| `hash_only` | Store instruction hashes but never instruction text |
| `issuers[].issuer` | The token issuer, such as `https://token.actions.githubusercontent.com`, a GitLab instance URL, `https://<jenkins>/oidc` or `https://vstoken.dev.azure.com/<organization id>` |
| `issuers[].provider` | `github`, `gitlab`, `jenkins` or `azure-devops`, which selects the claim mapping |
| `issuers[].audience` | Audience the token must carry (default `marshal`; `api://AzureADTokenExchange` for `azure-devops`, which can't be changed) |
| `issuers[].repositories` | Required. Glob patterns of callers allowed to write: repositories, Jenkins job URLs or Azure DevOps subjects |
| `issuers[].owner` | `caller` (default) or `parent`, for `jenkins` and `azure-devops`: identify callers by the job's parent or by the Azure DevOps project. See [Jenkins and Azure DevOps](other-ci.md) |
| `issuers[].jwks_uri` | Signing key location; discovered from the issuer when not set |
| `policy.shipped.refs` | Git refs that may record `shipped` events (default `refs/heads/main`) |
| `policy.shipped.environments` | CI deployment environments that may record `shipped` events |
| `policy.shipped.protected_refs` | Accept any ref the provider reports as protected (GitLab) |
| `policy.shipped.protected_environments` | Accept any environment the provider reports as protected (GitLab) |
| `policy.shipped.workflows` | Glob patterns of caller workflows that may record `shipped` events: a Jenkins job URL, an Azure DevOps subject, a GitHub workflow ref or a GitLab CI config ref |

`repositories` is required because a public provider signs tokens for every repository it hosts. The issuer alone proves only that a token came from GitHub, not from your organization. The same holds within an organization: a Jenkins controller signs tokens for every job, and an Azure DevOps organization for every pipeline.

Each provider names the caller with one claim, which the allowlist matches:

| Provider | Caller | Ref | Commit | Run |
|---|---|---|---|---|
| `github` | `repository` | `ref` | `sha` | `run_id`/`run_attempt` |
| `gitlab` | `project_path` | `ref`, as a full ref | `sha` | `pipeline_id` |
| `jenkins` | `sub` (the job URL) | `ref`, if a claim template adds it | `sha`, if a claim template adds it | `build_number` |
| `azure-devops` | `sub` (`p://` or `sc://<organization>/<project>/<name>`) | none | none | none |

A token without the caller claim is refused with `401`.

## Request

```http
POST /v0/events
Authorization: Bearer <OIDC token>
Content-Type: application/json

{
  "event": "proposed",
  "environment": "prod",
  "records": [ <AgentVersion>, ... ],
  "entities": [ <Entity>, ... ]
}
```

`marshal push` builds this request from an artifact and obtains the token automatically in GitHub Actions. For Jenkins and Azure DevOps, see [Jenkins and Azure DevOps](other-ci.md). In GitLab, configure an ID token and pass its variable name:

```yaml
record-agents:
  id_tokens:
    MARSHAL_ID_TOKEN:
      aud: marshal
  script:
    - pip install marshal-ledger
    - marshal push . --target prod --event shipped --url "$MARSHAL_URL" --token-env MARSHAL_ID_TOKEN
```

The response lists one report per agent: its version label, the version last shipped to the environment, and the risk-ranked changes between them.

## What the server checks

| Check | Failure |
|---|---|
| A bearer token is present, correctly signed by a configured issuer, with the right audience and unexpired | `401` |
| The token names a caller, and it matches the issuer's `repositories` | `401` without one, `403` if it doesn't match |
| For `shipped` and `observed`: the token's ref, environment or workflow satisfies `policy.shipped` | `403` |
| Each agent is unbound, or bound to the same issuer and repository | `403` (nothing is written) |
| The body is a valid event | `422` |

The server sets each event's channel: `ci` for `proposed` and `shipped` events, `scheduled` for `observed` events, which a scheduled job sends after exporting a platform's live definition. An `observed` event that differs from what was last shipped is reported as drift.

The server never trusts values a client could forge. It recomputes every version hash and quality score, re-runs credential redaction, applies `hash_only`, and records the commit and pipeline run from the token where the token carries them. Each event stores the caller's issuer, repository, ref and workflow, which `marshal log` shows.

## Signing keys and egress

The server fetches each issuer's signing keys: first `<issuer>/.well-known/openid-configuration`, then the `jwks_uri` it names, unless `jwks_uri` is configured. Keys are cached for an hour. A token signed with a key id the cache lacks causes a new fetch at most once a minute, so forged key ids can't make the server fetch on every request.

Fetches use Python's standard library:

- **Proxies.** `HTTPS_PROXY`, `HTTP_PROXY` and `NO_PROXY` (or their lowercase forms) apply, read at each fetch.
- **Certificates.** The server trusts OpenSSL's default certificate store. `SSL_CERT_FILE` (a PEM bundle) and `SSL_CERT_DIR` replace the default file and directory, so a bundle with an internal CA lets the server reach issuers on internal hosts. If the server also verifies tokens from public issuers such as GitHub, the bundle needs the public CAs too. `REQUESTS_CA_BUNDLE` is not read.

Issuers on internal hosts, such as GitHub Enterprise Server, a self-managed GitLab instance or Jenkins, need to be reachable from the server, through the proxy or directly. Where the issuer's discovery document isn't reachable but its keys are published elsewhere, set `jwks_uri` to that location. Setting `jwks_uri` also skips discovery.

## Agent ownership

An agent id is bound to the repository that first records it. For Jenkins and Azure DevOps that is the caller from the table above, or its parent with `owner: parent`. An administrator can move an agent to another repository with the store API (`Store.transfer`). Because binding happens on first write, any allowed repository can claim an agent id that has never been recorded. Keep `repositories` as narrow as your organization allows.

## Web view

Enable the read-only web view in the server configuration:

```yaml
web:
  enabled: true
  user_header: X-Forwarded-Email   # set by your SSO proxy after sign-in
```

The view is served by the same process: the inventory at `/`, agent pages, "who depends on this?", and a read-only JSON API under `/v0/agents`, `/v0/agent` and `/v0/deps`. It relies on the organization's SSO proxy (for example oauth2-proxy, Identity-Aware Proxy or Azure App Proxy) for sign-in. Requests without the user header are refused, so expose the view only through that proxy. Pages use no external resources.

For a local look at a store, `marshal web --db marshal.sqlite` serves the view on `127.0.0.1` without sign-in. Serving beyond the local machine requires `--user-header`.
