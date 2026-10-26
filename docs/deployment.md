# Deployment

Marshal runs as one service: the ingest API that pipelines push to, and the read-only web view. It needs a store and, in a shared deployment, the organization's SSO proxy in front of the web view.

| Store | Use |
|---|---|
| SQLite file | Trials and single-team use: one replica, a persistent volume |
| Postgres | Shared deployments: any number of replicas |

The server configuration names the store with `store:`, as a file path, a `postgresql://` URL, or `env:VARIABLE` to read either from the environment. Use the `env:` form to keep database credentials out of the configuration file.

## Container

Each release publishes a minimal image that runs as an unprivileged user:

```console
$ docker run -p 8080:8080 \
    -v ./marshal-server.yaml:/etc/marshal/marshal-server.yaml:ro \
    -e MARSHAL_STORE_URL=postgresql://marshal:...@db.internal:5432/marshal \
    ghcr.io/marshal-ledger/marshal:0.1.0
```

To run from your own registry, mirror the release image, or build one from a release tag with the [`Dockerfile`](../Dockerfile):

```console
$ git checkout v0.1.0
$ docker build -t registry.example.com/marshal:0.1.0 .
```

The Helm chart uses the release image by default, tagged with the chart's `appVersion`. Each release also attaches the packaged chart.

`/healthz` answers when the service is up. The image works with a read-only root filesystem when `/tmp` is writable.

## Kubernetes

The Helm chart in [`charts/marshal`](../charts/marshal) deploys the service with a ConfigMap for the configuration and a Secret for the store location:

```console
$ kubectl create secret generic marshal-store --from-literal=url='postgresql://marshal:...@db.internal:5432/marshal'
$ helm install marshal charts/marshal \
    --set store.existingSecret=marshal-store \
    -f my-values.yaml
```

`my-values.yaml` holds the server configuration under `config:`: OIDC issuers with their repository allowlists, the shipped policy, hash-only mode, and the web view. See [`values.yaml`](../charts/marshal/values.yaml) and the [ingest API guide](ingest-server.md). Pods run as non-root with a read-only root filesystem, no privilege escalation and all capabilities dropped. A SQLite store on a persistent volume is limited to one replica, and the chart refuses more.

## Network

Marshal never starts a connection to an agent platform (see [Network posture](design.md#network-posture)). It needs:

- **Inbound from CI runners:** `POST /v0/events`. For hosted runners, expose only this path through your ingress.
- **Inbound from users:** the web view at `/` and the read-only JSON API under `/v0/`, only through your SSO proxy. The proxy sets the configured user header (default `X-Forwarded-Email`), and requests without it are refused.
- **Outbound to the OIDC issuers:** to fetch their signing keys, for example `token.actions.githubusercontent.com`. Set `jwks_uri` per issuer to use a mirror.
- **Outbound to the store.**
