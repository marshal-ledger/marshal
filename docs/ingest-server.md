# Ingest API

The ingest API is how pipelines write to a shared Marshal store. It authenticates each request with the CI provider's OIDC token, applies the ingestion policy, re-normalizes the records and writes them. It is the only component that holds store credentials. For the design rationale, see [Ingestion and authentication](design.md#ingestion-and-authentication).

## Run

```console
$ pip install 'marshal-ledger[server]'
$ marshal serve --config marshal-server.yaml --host 0.0.0.0 --port 8080
```

Run it behind the organization's ingress or load balancer, which terminates TLS and enforces a request size limit. Only `POST /v0/events` and `GET /healthz` need to be reachable from CI runners.

## Configure

See [`examples/marshal-server.yaml`](../examples/marshal-server.yaml).

| Key | Meaning |
|---|---|
| `store` | Path of the SQLite store |
| `hash_only` | Store instruction hashes but never instruction text |
| `issuers[].issuer` | The token issuer, such as `https://token.actions.githubusercontent.com` or a GitLab instance URL |
| `issuers[].provider` | `github` or `gitlab`, which selects the claim mapping |
| `issuers[].audience` | Audience the token must carry (default `marshal`) |
| `issuers[].repositories` | Required. Glob patterns of repositories allowed to write |
| `issuers[].jwks_uri` | Signing key location; discovered from the issuer when not set |
| `policy.shipped.refs` | Git refs that may record `shipped` events (default `refs/heads/main`) |
| `policy.shipped.environments` | CI deployment environments that may record `shipped` events |
| `policy.shipped.protected_refs` | Accept any ref the provider reports as protected (GitLab) |
| `policy.shipped.protected_environments` | Accept any environment the provider reports as protected (GitLab) |

`repositories` is required because a public provider signs tokens for every repository it hosts. The issuer alone proves only that a token came from GitHub, not from your organization.

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

`marshal push` builds this request from an artifact and obtains the token automatically in GitHub Actions. In GitLab, configure an ID token and pass its variable name:

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
| The token's repository matches the issuer's `repositories` | `403` |
| For `shipped` and `observed`: the token's ref or environment satisfies `policy.shipped` | `403` |
| Each agent is unbound, or bound to the same issuer and repository | `403` (nothing is written) |
| The body is a valid event | `422` |

The server sets each event's channel: `ci` for `proposed` and `shipped` events, `poller` for `observed` events, which a scheduled pipeline sends after reading a platform's live definition. An `observed` event that differs from what was last shipped is reported as drift.

The server never trusts values a client could forge. It recomputes every version hash and quality score, re-runs credential redaction, applies `hash_only`, and records the commit and pipeline run from the token where the token carries them. Each event stores the caller's issuer, repository, ref and workflow, which `marshal log` shows.

## Agent ownership

An agent id is bound to the repository that first records it. An administrator can move an agent to another repository with the store API (`Store.transfer`). Because binding happens on first write, any allowed repository can claim an agent id that has never been recorded. Keep `repositories` as narrow as your organization allows.

## Web view

Enable the read-only web view in the server configuration:

```yaml
web:
  enabled: true
  user_header: X-Forwarded-Email   # set by your SSO proxy after sign-in
```

The view is served by the same process: the inventory at `/`, agent pages, "who depends on this?", and a read-only JSON API under `/v0/agents`, `/v0/agent` and `/v0/deps`. It relies on the organization's SSO proxy (for example oauth2-proxy, Identity-Aware Proxy or Azure App Proxy) for sign-in. Requests without the user header are refused, so expose the view only through that proxy. Pages use no external resources.

For a local look at a store, `marshal web --db marshal.sqlite` serves the view on `127.0.0.1` without sign-in. Serving beyond the local machine requires `--user-header`.
