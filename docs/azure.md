---
title: "Run Marshal on Azure: AKS or Container Apps, PostgreSQL and Entra ID"
---

# Running Marshal on Azure

This guide covers a shared Marshal deployment on Azure: the store on Azure Database for PostgreSQL flexible server, the ingest API and web view on AKS or Azure Container Apps, sign-in to the web view with Microsoft Entra ID, and secrets in Key Vault. Read [Deployment](deployment.md) and [Ingest API](ingest-server.md) first; this page adds only what is specific to Azure. Azure Pipelines push with their own OIDC tokens; see [Jenkins and Azure DevOps](other-ci.md#azure-devops).

Signing in to the database with Microsoft Entra ID (`store_auth: azure`), and the chart's `serviceAccount` and `podLabels` values, need Marshal 0.2.0 or later.

## The store: Azure Database for PostgreSQL

Use a flexible server and one database for Marshal. The ingest API creates the tables on its first connection and migrates them on upgrade.

The store location is a libpq URL:

```text
postgresql://<user>:<password>@<server>.postgres.database.azure.com:5432/marshal?sslmode=require&connect_timeout=10
```

- `sslmode=require` encrypts the connection. To also verify the server certificate, use `sslmode=verify-full` and set `sslrootcert` to a file holding the root certificates Azure documents for flexible server.
- `connect_timeout` bounds how long a request waits for a server that doesn't answer, such as during a failover.
- Percent-encode special characters in the user name and password.

### What happens when the connection drops

The ingest API and the web view open a database connection for each request, so a failover or a dropped connection affects only the requests in flight; the next request connects again. Within a request, a lost connection is replaced: a read is retried once on the new connection, while a write is never replayed. While the database can't be reached, the ingest API and the web view answer `503` with `Retry-After: 5`, and command-line tools exit with an error. A write interrupted by a lost connection is rolled back by the database, unless the connection was lost while committing, when its outcome is unknown: re-running the push then succeeds, but may record the event twice.

### Password sign-in

Put the URL, password included, in a secret (see [Secrets in Key Vault](#secrets-in-key-vault)) and point `store` at it: `store: env:MARSHAL_STORE_URL`, which is what the Helm chart does.

### Microsoft Entra ID sign-in

With `store_auth: azure`, Marshal signs in with a Microsoft Entra ID access token instead of a password. It gets the token from azure-identity's `DefaultAzureCredential`, which uses the AKS workload identity or the managed identity the app runs as, and requests the scope `https://ossrdbms-aad.database.windows.net/.default`. A token is fetched when the store connects and reused for later connections until five minutes before it expires. Postgres checks the token only at sign-in, so open connections are unaffected when it expires. Marshal never logs or stores the token.

1. The container image includes azure-identity. Outside the image, install the extra: `pip install 'marshal-ledger[server,postgres,azure]'`.
2. On the server, enable Microsoft Entra authentication and add a Microsoft Entra administrator (the server's **Authentication** page in the portal).
3. Create a user-assigned managed identity for the ingest API, and one for the web view if you run it separately (see [Container Apps](#azure-container-apps)).
4. Connected as the Entra administrator, create a database role for each identity. The role name must be the identity's display name:

   ```sql
   SELECT * FROM pgaadauth_create_principal('marshal-ingest', false, false);
   SELECT * FROM pgaadauth_create_principal('marshal-web', false, false);
   CREATE DATABASE marshal;
   ```

5. Connected to the `marshal` database as the administrator, let the ingest API create tables and the web view read them:

   ```sql
   GRANT CREATE, USAGE ON SCHEMA public TO "marshal-ingest";
   GRANT USAGE ON SCHEMA public TO "marshal-web";
   ALTER DEFAULT PRIVILEGES FOR ROLE "marshal-ingest" IN SCHEMA public GRANT SELECT ON TABLES TO "marshal-web";
   ```

   The web view only reads, so its role needs no write privileges. Start the ingest API first after an upgrade, since it applies schema migrations.

6. Give the store URL with the role name as the user and no password, and set `store_auth`:

   ```yaml
   # marshal-server.yaml
   store: env:MARSHAL_STORE_URL   # postgresql://marshal-ingest@<server>.postgres.database.azure.com:5432/marshal?sslmode=require&connect_timeout=10
   store_auth: azure
   ```

   For command-line tools and `marshal web`, use `--db-auth azure` or `MARSHAL_DB_AUTH=azure` with `--db` or `MARSHAL_DB`.

7. If the app runs as a user-assigned managed identity outside AKS workload identity, set `AZURE_CLIENT_ID` to the identity's client ID; `DefaultAzureCredential` reads it to choose the identity.

`store_auth` also accepts `module:callable`, a function of your own that takes no arguments and returns the password for the next connection, for other short-lived credentials.

## The image

Import the release image into your registry, so clusters pull from Azure Container Registry:

```console
$ az acr import --name myregistry --source ghcr.io/marshal-ledger/marshal:<version> --image marshal:<version>
```

To check the image's signature and provenance before importing it, see [Deployment](deployment.md#verify-and-mirror).

## AKS with the Helm chart

The chart in [`charts/marshal`](../charts/marshal) works on AKS as on any cluster. Set `image.repository` to your registry, and `store.existingSecret` to a Secret holding the store URL.

For password sign-in, nothing else is Azure-specific.

For Entra ID sign-in with [workload identity](https://learn.microsoft.com/azure/aks/workload-identity-overview), enable workload identity and the OIDC issuer on the cluster, and add a federated identity credential on the managed identity for the chart's service account, `system:serviceaccount:<namespace>:<name>`. The name is `serviceAccount.name` if set, otherwise the release's full name (`marshal` for a release named `marshal`). Then:

```yaml
# my-values.yaml
serviceAccount:
  create: true
  annotations:
    azure.workload.identity/client-id: <managed identity client ID>
podLabels:
  azure.workload.identity/use: "true"
config:
  store_auth: azure
  # issuers, policy, web ...
```

The Secret named by `store.existingSecret` holds the URL with the identity's role name as the user and no password. The workload identity webhook gives the pod the identity's client ID and a federated token, which `DefaultAzureCredential` uses.

### Web view sign-in on AKS

The web view trusts the user header from anyone who reaches it, so the SSO proxy must be the only route to the web paths. One way is [oauth2-proxy](https://oauth2-proxy.github.io/oauth2-proxy/) with its Microsoft Entra ID provider (`--provider=entra-id`), run as a reverse proxy with the Marshal service as its upstream. oauth2-proxy passes `X-Forwarded-Email` to the upstream by default (`--pass-user-headers`), which is the web view's default `user_header`.

The ingest API and the web view are served by the same process. Route only `POST /v0/events` and `GET /healthz` from the ingress to Marshal directly, with exact path matches, and send every other path through oauth2-proxy. Keep the service `ClusterIP`, and enable the chart's NetworkPolicy so that only the ingress controller and the proxy reach the pods ([Deployment](deployment.md#network-policy)).

## Azure Container Apps

On Container Apps, run the ingest API and the web view as two apps from the same image. Each gets its own managed identity, and the web view's identity has read-only database access.

**Ingest API.** External ingress on port 8080, without built-in authentication: CI runners authenticate with their OIDC tokens. The image's default command reads `/etc/marshal/marshal-server.yaml`; supply it as a secret mounted as a file, and the store URL as an environment variable:

```yaml
properties:
  configuration:
    ingress: {external: true, targetPort: 8080}
    secrets:
      - name: server-config
        value: |
          store: env:MARSHAL_STORE_URL
          store_auth: azure
          issuers: [...]
      - name: store-url
        keyVaultUrl: https://<vault>.vault.azure.net/secrets/marshal-store-url
        identity: <identity resource ID>
  template:
    containers:
      - name: marshal
        image: myregistry.azurecr.io/marshal:<version>
        env:
          - {name: MARSHAL_STORE_URL, secretRef: store-url}
          - {name: AZURE_CLIENT_ID, value: <identity client ID>}
        volumeMounts:
          - {volumeName: config, mountPath: /etc/marshal}
    volumes:
      - name: config
        storageType: Secret
        secrets:
          - {secretRef: server-config, path: marshal-server.yaml}
```

**Web view.** Run `marshal web` with built-in authentication ("Easy Auth") and the Microsoft identity provider, with *Restrict access* set to **Require authentication**. Easy Auth sets `X-MS-CLIENT-PRINCIPAL-NAME` to the signed-in user's name, usually their user principal name, and doesn't let external requests set it:

```yaml
    containers:
      - name: marshal-web
        image: myregistry.azurecr.io/marshal:<version>
        args: ["web", "--host", "0.0.0.0", "--port", "8080", "--user-header", "X-MS-CLIENT-PRINCIPAL-NAME"]
        env:
          - {name: MARSHAL_DB, secretRef: store-url-web}   # postgresql://marshal-web@...
          - {name: MARSHAL_DB_AUTH, value: azure}
          - {name: AZURE_CLIENT_ID, value: <web identity client ID>}
```

Easy Auth protects apps with external ingress. Give the web app no other route that bypasses it. App Service authentication sets the same header, so the same `--user-header` applies when the web view runs on App Service.

## Secrets in Key Vault

Keep the store URL, and any password in it, in Key Vault rather than in configuration files. With Entra ID sign-in the URL holds no password, but it still names the server and role.

- **Container Apps:** reference the Key Vault secret from the app's secrets (`keyVaultUrl` and `identity`, as above), and grant the app's identity the **Key Vault Secrets User** role.
- **AKS:** the chart reads the URL from a Kubernetes Secret (`store.existingSecret`). Create that Secret from Key Vault with a controller such as External Secrets Operator. The Secrets Store CSI driver creates a Kubernetes Secret only while a pod mounts its volume, which the chart's pods don't.

## Don't put SQLite on Azure Files

Use Postgres for any shared deployment on Azure. A SQLite store on an Azure Files share (SMB or NFS) depends on file locking over the network, which [SQLite advises against](https://www.sqlite.org/useovernet.html) and which can corrupt the database. More than one replica also needs Postgres. SQLite suits a single replica on a local or block-storage disk, such as an AKS managed disk.
