# Deployment

Marshal runs as one service: the ingest API that pipelines push to, and the read-only web view. It needs a store and, in a shared deployment, the organization's SSO proxy in front of the web view.

| Store | Use |
|---|---|
| SQLite file | Trials and single-team use: one replica, a persistent volume |
| Postgres | Shared deployments: any number of replicas |

The server configuration names the store with `store:`, as a file path, a `postgresql://` URL, or `env:VARIABLE` to read either from the environment. Use the `env:` form to keep database credentials out of the configuration file.

For what Marshal stores, how it authenticates callers and what it connects to, see the [security review](security-review.md). For Azure Database for PostgreSQL, AKS with workload identity, Container Apps and Entra ID sign-in, see [Running Marshal on Azure](azure.md).

## Container

Each release publishes a minimal image that runs as an unprivileged user:

```console
$ docker run -p 8080:8080 \
    -v ./marshal-server.yaml:/etc/marshal/marshal-server.yaml:ro \
    -e MARSHAL_STORE_URL=postgresql://marshal:...@db.internal:5432/marshal \
    ghcr.io/marshal-ledger/marshal:0.4.0
```

To run from your own registry, mirror the release image, or build one from a release tag with the [`Dockerfile`](../Dockerfile):

```console
$ git checkout v0.4.0
$ docker build -t registry.example.com/marshal:0.4.0 .
```

The Helm chart uses the release image by default, tagged with the chart's `appVersion`. Each release also attaches the packaged chart.

The image runs as UID 10001 by default, and also as any other UID in group 0, which is how OpenShift runs containers. It writes nothing outside its volumes, so it works with a read-only root filesystem. A SQLite store goes on a volume at `/var/lib/marshal`, which is writable by group 0. `/healthz` answers when the service is up.

### Verify and mirror

Release images are built for `linux/amd64` and `linux/arm64`, carry an SPDX software bill of materials and SLSA build provenance, and are signed with Sigstore by the release workflow. To verify a signature before admitting an image:

```console
$ cosign verify ghcr.io/marshal-ledger/marshal:0.4.0 \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com \
    --certificate-identity-regexp '^https://github.com/marshal-ledger/marshal/\.github/workflows/release\.yml@refs/tags/v'
```

The same identity works in admission policies such as Kyverno or Sigstore's policy-controller.

Clusters that can't reach `ghcr.io` can pull through a registry proxy (Artifactory, Harbor, Nexus, or ECR and ACR pull-through caches), or use a copy in an internal registry. `cosign copy` copies the image with its signature:

```console
$ cosign copy ghcr.io/marshal-ledger/marshal:0.4.0 registry.example.com/marshal:0.4.0
$ cosign verify registry.example.com/marshal:0.4.0 \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com \
    --certificate-identity-regexp '^https://github.com/marshal-ledger/marshal/\.github/workflows/release\.yml@refs/tags/v'
```

The copy keeps the image digest, so the signature still matches it. The SBOM and provenance are part of the image index, so they come along too; `docker buildx imagetools inspect registry.example.com/marshal:0.4.0 --format '{{json .SBOM}}'` shows them. A tool that copies only the image, such as `skopeo copy --all`, `oc image mirror` or `az acr import`, brings the SBOM and provenance but not the signature, which cosign keeps under a separate `sha256-<digest>.sig` tag. Copy that tag as well, or use `cosign copy`.

`cosign verify` needs Sigstore's trust root, which it downloads from `tuf-repo-cdn.sigstore.dev` even with `--offline`. Run verification on a host that can reach it, before or after copying, or give your admission controller a mirrored trust root as its documentation describes. Then set `image.repository` in the chart. You can also pin the digest the signature covers, for example `image.tag: 0.4.0@sha256:...`.

## Kubernetes

The Helm chart in [`charts/marshal`](../charts/marshal) deploys the service with a ConfigMap for the configuration and a Secret for the store location:

```console
$ kubectl create secret generic marshal-store --from-literal=url='postgresql://marshal:...@db.internal:5432/marshal'
$ helm install marshal charts/marshal \
    --set store.existingSecret=marshal-store \
    -f my-values.yaml
```

`my-values.yaml` holds the server configuration under `config:`: OIDC issuers with their repository allowlists, the shipped policy, hash-only mode, and the web view. See [`values.yaml`](../charts/marshal/values.yaml), the [ingest API guide](ingest-server.md) and, for Jenkins and Azure DevOps pipelines, [Jenkins and Azure DevOps](other-ci.md). Pods run as non-root with a read-only root filesystem, no privilege escalation and all capabilities dropped. A SQLite store on a persistent volume is limited to one replica, and the chart refuses more.

### OpenShift

OpenShift's `restricted-v2` security context constraint assigns each pod a UID and fsGroup from the namespace's range, and rejects pods that ask for others. Leave both to it by setting them empty (chart version 0.2.0 and later). An empty value leaves the field out of the pod spec:

```yaml
podSecurityContext:
  runAsUser: ""
  fsGroup: ""

route:
  enabled: true
  host: marshal.apps.example.com   # or leave empty for the router's default
```

The rest of the defaults (`runAsNonRoot`, the `RuntimeDefault` seccomp profile, a read-only root filesystem, no privilege escalation, all capabilities dropped) are within `restricted-v2`, so the pod needs no extra SCC. With a persistent volume for SQLite, the SCC's fsGroup makes the volume writable.

The Route terminates TLS at the router (`route.tls`, edge termination with HTTP redirected by default) and sends traffic to the service. An Ingress (`ingress.enabled`) also works on OpenShift, which turns it into a Route. Expose the web view only through your SSO proxy, as below. If the proxy runs in the cluster and forwards to the service directly, you may not need a Route at all.

### Network policy

The web view trusts the user header from any client that reaches the pod (see [Network](#network)). With `networkPolicy.enabled` (chart version 0.2.0 and later), the chart adds a NetworkPolicy that admits traffic to the pod only from the peers you list, typically the ingress controller and the SSO proxy:

```yaml
networkPolicy:
  enabled: true
  from:
    - namespaceSelector:
        matchLabels: {kubernetes.io/metadata.name: ingress-nginx}
    - namespaceSelector:
        matchLabels: {kubernetes.io/metadata.name: sso-proxy}
```

On OpenShift, the router's namespace is selected with `policy-group.network.openshift.io/ingress: ""`. The policy only restricts traffic coming into the pod. Outbound traffic (the store and the OIDC issuers' keys, below) is left to your cluster's egress rules.

## Locked-down installs

Marshal installs without internet access once three things are in your own systems: the container image (see [Verify and mirror](#verify-and-mirror)), the Helm chart, and, for pipelines, the Python package.

**Helm chart.** Each GitHub release attaches the packaged chart, `marshal-<chart version>.tgz` (`marshal-0.4.0.tgz` for release 0.4.0). Push it to an internal chart repository or OCI registry, for example `helm push marshal-0.4.0.tgz oci://registry.example.com/charts`, or install from a checkout of the release tag.

**Python package.** Pipelines that run `marshal push` install `marshal-ledger` from PyPI. To use an internal index such as Artifactory, Nexus or devpi, point pip at it with an environment variable or `pip.conf`:

```console
$ export PIP_INDEX_URL=https://pypi.internal.example/simple
$ pip install marshal-ledger
```

```ini
# /etc/pip.conf, or ~/.config/pip/pip.conf for one user
[global]
index-url = https://pypi.internal.example/simple
```

The client needs `marshal-ledger` with `pydantic` and `pyyaml` and their dependencies. The server also needs the `server` and `postgres` extras (`fastapi`, `uvicorn`, `pyjwt[crypto]`, `psycopg[binary]`), and the `azure` extra (`azure-identity`) for Entra ID sign-in to the store, but the image already contains them. Installing the published wheel needs no build tools. The [GitHub Action](github-action.md#runners-without-internet-access) is different: it builds Marshal from its own source, so the index must also serve the build backends `hatchling` and `hatch-fancy-pypi-readme`.

## Network

Marshal never starts a connection to an agent platform (see [Network posture](design.md#network-posture)). It needs:

- **Inbound from CI runners:** `POST /v0/events`. For hosted runners, expose only this path through your ingress.
- **Inbound from users:** the web view at `/` and the read-only JSON API under `/v0/`, only through your SSO proxy. The proxy sets the configured user header (default `X-Forwarded-Email`), and requests without it are refused. The proxy must replace any user header the client sent, and must be the only route to the web view: anything else that reaches the pod can set the header itself.
- **Outbound to the OIDC issuers:** to fetch their signing keys, for example `token.actions.githubusercontent.com`, a Jenkins controller (`<jenkins>/oidc`) or `vstoken.dev.azure.com`. Without `jwks_uri`, the server reads `<issuer>/.well-known/openid-configuration` and then the key set it names. Set `jwks_uri` per issuer to fetch the keys from a mirror instead. These fetches honor `HTTPS_PROXY`, `HTTP_PROXY` and `NO_PROXY`, and `SSL_CERT_FILE` for an internal CA ([Signing keys and egress](ingest-server.md#signing-keys-and-egress)).
- **Outbound to the store.** With `store_auth: azure`, also to Microsoft Entra ID or the managed identity endpoint for database tokens.

The server speaks plain HTTP on port 8080. Terminate TLS at the ingress, Route or proxy. For TLS to Postgres, add `sslmode=verify-full` (and `sslrootcert` if needed) to the store URL.
