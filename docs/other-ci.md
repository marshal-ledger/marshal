---
title: "Jenkins and Azure DevOps: record AI agents from pipelines"
---

# Jenkins and Azure DevOps

Pipelines on Jenkins and Azure DevOps authenticate to the [ingest API](ingest-server.md) the same way GitHub and GitLab pipelines do: with a short-lived OIDC token signed by the CI system. The pipeline puts the token in an environment variable and runs `marshal push --token-env`. No secret is stored for Marshal.

Neither system's token names a Git ref the way GitHub's and GitLab's do, so the shipped policy matches the caller's job or pipeline instead, with `policy.shipped.workflows`.

## Jenkins

Jenkins issues tokens with the [OpenID Connect Provider plugin](https://plugins.jenkins.io/oidc-provider/) (`oidc-provider`).

### Tokens

A token for a build carries these claims by default:

| Claim | Value | Marshal records it as |
|---|---|---|
| `iss` | The Jenkins URL followed by `oidc`, such as `https://jenkins.acme.example/oidc`. A credential defined in a folder has its own issuer: `https://jenkins.acme.example/oidc/job/<folder>` | issuer |
| `aud` | The audience set on the credential | |
| `sub` | The job URL, such as `https://jenkins.acme.example/job/agents/job/finance/job/main/` | the caller, and its workflow |
| `build_number` | The build number | the pipeline run, as `jenkins:<number>` |

Jenkins serves the signing keys without authentication at `<issuer>/.well-known/openid-configuration` and `<issuer>/jwks`, so the Marshal server needs only network access to Jenkins. If a credential is given a different issuer URI, Jenkins no longer serves its keys; publish them at that URI as the plugin describes, and set `jwks_uri` if discovery isn't available there.

### Set up the credential

1. Install the OpenID Connect Provider plugin.
2. Add a credential of kind **OpenID Connect id token**, with ID `marshal-id-token` and audience `marshal`. Add it at the root, or in the folder that holds the agent projects to give those projects their own issuer.
3. In **Manage Jenkins → Configure Global Security → OpenID Connect**, keep the default build claim templates (`sub` and `build_number`). Tokens last an hour by default.

Bind the credential as secret text and push:

```groovy
pipeline {
  agent any
  environment {
    MARSHAL_URL = 'https://marshal.acme.example'
  }
  stages {
    stage('Record agents') {
      steps {
        withCredentials([string(credentialsId: 'marshal-id-token', variable: 'MARSHAL_ID_TOKEN')]) {
          sh '''
            pip install marshal-ledger
            EVENT=proposed
            if [ "$BRANCH_NAME" = main ]; then EVENT=shipped; fi
            marshal push . --target prod --event "$EVENT" --commit "$(git rev-parse HEAD)" \
              --url "$MARSHAL_URL" --token-env MARSHAL_ID_TOKEN
          '''
        }
      }
    }
  }
}
```

Each `withCredentials` block gets a new token for that build.

### Configure the server

```yaml
issuers:
  - issuer: https://jenkins.acme.example/oidc
    provider: jenkins
    audience: marshal
    owner: parent          # every branch job of a multibranch project is one caller
    repositories:
      - https://jenkins.acme.example/job/agents/*

policy:
  shipped:
    workflows:
      - https://jenkins.acme.example/job/agents/job/*/job/main/
```

`repositories` lists job URLs. A pattern's `*` also matches `/`, so `https://jenkins.acme.example/job/agents/*` admits every job in the `agents` folder and in the folders under it.

In a multibranch project, each branch and each pull request is its own job with its own URL. Marshal binds an agent to the first caller that records it, so without `owner: parent` a pull request job would own the agent and the main branch's job couldn't ship it. With `owner: parent`, the caller is the job's parent, here `https://jenkins.acme.example/job/agents/job/finance/`, and the allowlist and ownership apply to that. A job directly under the Jenkins root has no parent and is refused. Every branch and pull request job in the project can then record `proposed` events for the project's agents.

`policy.shipped.workflows` matches the full job URL, which Marshal records as the caller's workflow. The example lets only each project's `main` job record `shipped` and `observed` events.

### Claim templates

Administrators can add claims in **Build claim templates**. Marshal reads two optional ones:

| Claim | Read as |
|---|---|
| `ref` | The Git ref, matched against `policy.shipped.refs` |
| `sha` | The commit, recorded instead of the one the request names |

The plugin fills templates from variables Jenkins defines for the job and the build, such as `JOB_NAME`, `BUILD_NUMBER`, and in multibranch projects `BRANCH_NAME`, `CHANGE_ID` and `TAG_NAME`. Variables set by steps during the build, such as the Git plugin's `GIT_COMMIT` and `GIT_URL`, are not among them. The plugin refuses to issue any token when a template names a variable the build doesn't define, and build templates apply to every build on the controller. A template such as `ref` = `refs/heads/${BRANCH_NAME}` therefore breaks tokens for jobs outside multibranch projects, and in pull request jobs it yields a name such as `refs/heads/PR-12`. `policy.shipped.workflows` avoids both problems.

Without a `sha` claim, the server records the commit the request names (`--commit`), which comes from the pipeline rather than from Jenkins.

## Azure DevOps

Azure Pipelines issues tokens from the Azure DevOps token service. A job requests one from the OIDC token endpoint with its `System.AccessToken`.

### Tokens

| Claim | Value | Marshal records it as |
|---|---|---|
| `iss` | `https://vstoken.dev.azure.com/<organization id>`, where the organization id is a GUID | issuer |
| `aud` | Always `api://AzureADTokenExchange` | |
| `sub` | `p://<organization>/<project>/<pipeline>` for a pipeline's own token, or `sc://<organization>/<project>/<service connection>` for a service connection's | the caller, and its workflow |

The token names no branch, commit or run. Marshal records the commit and run the request names (`--commit`, `--pipeline-run`), which come from the pipeline rather than from Azure DevOps. The subject holds names, not ids: renaming the organization, project, pipeline or service connection changes the caller.

The organization id is the `id` that `https://dev.azure.com/<organization>/_apis/projectCollections` returns.

### Push from a pipeline

A job gets its pipeline's token by posting to the address in `System.OidcRequestUri`:

```yaml
steps:
  - script: |
      pip install marshal-ledger
      MARSHAL_ID_TOKEN=$(curl -sSf -X POST -d '' \
        -H "Authorization: Bearer $SYSTEM_ACCESSTOKEN" -H "Content-Type: application/json" \
        "$SYSTEM_OIDCREQUESTURI?api-version=7.1" \
        | python3 -c 'import json, sys; print(json.load(sys.stdin)["oidcToken"])')
      export MARSHAL_ID_TOKEN
      marshal push . --target prod --event proposed \
        --commit "$(Build.SourceVersion)" --pipeline-run "$(Build.BuildId)" \
        --url "$MARSHAL_URL" --token-env MARSHAL_ID_TOKEN
    displayName: Record agents
    env:
      SYSTEM_ACCESSTOKEN: $(System.AccessToken)
```

`System.AccessToken` is a secret, so it reaches the script only when mapped in `env`.

### Configure the server

```yaml
issuers:
  - issuer: https://vstoken.dev.azure.com/0d6c2f3e-8b1a-4c5e-9f7d-2a4b6c8e0f13
    provider: azure-devops
    repositories:
      - p://acme/agents/*

policy:
  shipped:
    workflows:
      - p://acme/agents/release-agents
```

`audience` defaults to `api://AzureADTokenExchange` for this provider, because Azure DevOps sets it and a pipeline can't choose another. The issuer covers one organization, and its tokens are accepted only from callers on the allowlist.

The pipeline's token is the same for every run of a pipeline, whatever its branch. To let a pipeline record `shipped` events, use a dedicated pipeline, list it in `policy.shipped.workflows`, and allow only release managers to queue or edit it: anyone who can queue it can run it on any branch.

With `owner: parent`, the caller is `<organization>/<project>` for both kinds of subject, so the allowlist reads `acme/agents`, and pipelines and service connections in the same project record the same agents. Any pipeline in an allowed project can then write.

### Service connection tokens

A service connection's token has the subject `sc://<organization>/<project>/<service connection>`. Use one when a check on the connection, such as branch control, should decide which runs may record `shipped` events: list its subject in `policy.shipped.workflows`. The `AzureCLI@2` task with `addSpnToEnvironment: true` exposes the token to its script as `idToken`, so `--token-env idToken` sends it.

Only service connections that use the Azure DevOps issuer produce these tokens. New workload identity service connections use the Microsoft Entra issuer by default, and Microsoft documents their tokens for exchange with Entra ID only. Marshal has not been tested with them.

### Audience and reuse

Every Azure DevOps token has the audience `api://AzureADTokenExchange`, so a token sent to Marshal is also one that any system federated with the same subject accepts: Microsoft Entra ID, a cloud provider, or a data platform. A service connection's token can be exchanged for that connection's Azure access. Whoever runs the Marshal server could use such a token until it expires. Prefer pipeline tokens and don't federate the pipeline's subject with anything else. If you use a service connection, create one only for Marshal and give its identity the narrowest Azure role its task allows.
