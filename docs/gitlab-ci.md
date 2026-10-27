# GitLab CI

GitLab pipelines record agent definitions the same way GitHub workflows do. A job reads the agent's artifacts, signs in to the [ingest API](ingest-server.md) with a short-lived ID token that GitLab issues, and records a `proposed` or `shipped` event. In merge request pipelines it also comments with the risk-ranked changes, and updates that comment on later pushes. No secret is stored for Marshal.

## Add it to a pipeline

Include the template from a Marshal release, then extend its `.marshal-collect` job. `v0` follows the latest 0.x release; to decide when to upgrade, include a release tag such as `v0.7.0` instead.

```yaml
include:
  - remote: https://raw.githubusercontent.com/marshal-ledger/marshal/v0/collect/gitlab-ci.yml

marshal-proposed:
  extends: .marshal-collect
  stage: test
  variables:
    MARSHAL_EVENT: proposed
    MARSHAL_URL: https://marshal.example.com
    MARSHAL_TARGET: prod
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"

marshal-shipped:
  extends: .marshal-collect
  stage: .post                # after the deploy job succeeds
  variables:
    MARSHAL_EVENT: shipped
    MARSHAL_URL: https://marshal.example.com
    MARSHAL_TARGET: prod
  rules:
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
```

For [Terraform](formats/terraform.md#in-a-pipeline), set `MARSHAL_ARTIFACTS` to the plan's JSON, written by an earlier job and passed along as an artifact.

| Variable | Default | Meaning |
|---|---|---|
| `MARSHAL_EVENT` | `proposed` | `proposed`, `shipped` or `observed` |
| `MARSHAL_URL` | | Base URL of the ingest API. Required |
| `MARSHAL_ARTIFACTS` | `.` | Path of the agent artifact, such as a bundle directory or a Terraform plan's JSON |
| `MARSHAL_TARGET` | | Deployment target whose overrides to apply |
| `MARSHAL_ENVIRONMENT` | `MARSHAL_TARGET` | Environment the event applies to |
| `MARSHAL_PORTABILITY` | | Space-separated platforms to score portability to in the report, such as `bedrock copilot-studio` |
| `MARSHAL_COMMENT` | `true` | Comment on merge requests, when `MARSHAL_GITLAB_TOKEN` is set |
| `MARSHAL_VERSION` | the template's release | Marshal release installed from PyPI |
| `MARSHAL_PACKAGE` | | What `pip` installs instead, such as a URL on an internal mirror |
| `MARSHAL_PYTHON_IMAGE` | `python:3.12-slim` | Image the job runs in |

The job installs Marshal from PyPI, so the runner needs PyPI or a mirror of it (see [Locked-down installs](deployment.md#locked-down-installs)). The report is printed in the job log and kept as the `marshal-report.md` artifact, which merge requests show as "Marshal report".

## Merge request comments

GitLab's job token can't write merge request comments, so commenting needs a token the project provides. Without one, the job records the event and keeps the report, and says that no comment was posted.

1. Create a token with the **api** scope that can comment on the project's merge requests:
   - **GitLab Self-Managed and Dedicated, or GitLab.com Premium and Ultimate:** a project access token (**Settings → Access tokens**) with the **Reporter** role.
   - **GitLab.com Free**, where project access tokens [aren't available](https://docs.gitlab.com/user/project/settings/project_access_tokens/): a personal access token of a dedicated bot account that is a Reporter on the project. A personal token acts as its user on every project that user can reach, so don't use your own.
2. Add it as a CI/CD variable named `MARSHAL_GITLAB_TOKEN` (**Settings → CI/CD → Variables**), masked, with an expiry date.

Merge request pipelines usually run on unprotected branches, which can't read protected variables, so leave the variable unprotected. Anyone who can change the pipeline can then use the token, so give its user the Reporter role and nothing more. The token is used only for the GitLab API of the instance running the pipeline (`CI_API_V4_URL`), and is never printed.

## Configure the server

Add your GitLab instance as an issuer in the server's [configuration](ingest-server.md):

```yaml
issuers:
  - issuer: https://gitlab.com          # or your self-managed instance's URL
    provider: gitlab
    audience: marshal
    repositories:
      - acme/agents/*                    # project paths allowed to record events

policy:
  shipped:
    protected_refs: true                 # shipped only from protected branches and tags
    protected_environments: true         # or from protected environments
```

An agent is bound to the project that first records it. `shipped` events are accepted only from refs or environments the policy allows, so a merge request can't mark its own version as live. To use another audience, set it in the server's issuer and override the job's `id_tokens`:

```yaml
marshal-proposed:
  extends: .marshal-collect
  id_tokens:
    MARSHAL_ID_TOKEN:
      aud: https://marshal.example.com
```

Merge requests from forks run in the fork's project, so their tokens name the fork and Marshal refuses them unless the fork is in `repositories`.

## Testing

The template is checked against GitLab's published `.gitlab-ci.yml` schema, pinned to a commit, and its script is run in its image with a stand-in for Marshal to check the arguments it passes. Comment posting is tested against a stand-in for GitLab's notes API.

**Tested on GitLab.com:** on 2026-10-27, a private GitLab.com project included the template from `v0`, with its jobs on a GitLab runner reaching a Marshal server configured for GitLab.com. A push to the protected `main` branch recorded `shipped`, with the commit, pipeline id and project taken from the GitLab ID token. A merge request recorded `proposed` and got one comment ranking the change, and later pushes updated that comment instead of adding more. That test found that `PRIVATE-TOKEN` refuses OAuth tokens, fixed in 0.7.1 by sending tokens as `Authorization: Bearer`.
