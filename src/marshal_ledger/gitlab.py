"""Posting a Marshal report as a GitLab merge request comment.

The GitLab CI template (``collect/gitlab-ci.yml``) runs ``marshal comment`` in merge
request pipelines. GitLab's job token can't write merge request notes, so this uses a
token the project provides in a CI/CD variable: a project access token, or on GitLab.com
Free a bot account's personal access token. The comment starts with a
marker, so a later pipeline updates it instead of adding another.

The request goes to the GitLab instance running the pipeline, named by GitLab's own
``CI_API_V4_URL``. Nothing else is contacted, and the token is never printed.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Optional
from urllib.parse import quote

from .report import COMMENT_MARKER


class GitLabError(Exception):
    pass


Opener = Callable[[urllib.request.Request], Any]


def _request(opener: Opener, method: str, url: str, token: str, body: Optional[dict[str, Any]] = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={"PRIVATE-TOKEN": token, "Content-Type": "application/json"})
    try:
        with opener(request) as response:
            return json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        raise GitLabError(f"GitLab API {method} {url.split('?')[0]} returned {exc.code}") from None
    except urllib.error.URLError as exc:
        raise GitLabError(f"GitLab API {method} {url.split('?')[0]} failed: {exc.reason}") from None


def post_report(body: str, *, api_url: str, project_id: str, merge_request: str, token: str,
                opener: Opener = urllib.request.urlopen) -> str:
    """Create or update the Marshal comment on a merge request. Returns ``created`` or ``updated``."""
    notes = f"{api_url.rstrip('/')}/projects/{quote(str(project_id), safe='')}/merge_requests/{merge_request}/notes"
    existing = None
    for page in range(1, 51):
        found = _request(opener, "GET", f"{notes}?per_page=100&page={page}&sort=asc", token) or []
        existing = next((n for n in found if not n.get("system") and str(n.get("body", "")).startswith(COMMENT_MARKER)), None)
        if existing or len(found) < 100:
            break
    if existing:
        _request(opener, "PUT", f"{notes}/{existing['id']}", token, {"body": body})
        return "updated"
    _request(opener, "POST", notes, token, {"body": body})
    return "created"


def post_from_pipeline(report_path: str, token_env: str, environ: Optional[dict[str, str]] = None,
                       opener: Opener = urllib.request.urlopen) -> str:
    """Post the report from a GitLab merge request pipeline, using GitLab's predefined variables.

    Returns what happened, as a sentence for the job log. Outside a merge request pipeline, or
    without a token, nothing is posted and the reason is returned instead of an error, so a
    missing optional token never fails a pipeline.
    """
    env = os.environ if environ is None else environ
    merge_request = env.get("CI_MERGE_REQUEST_IID")
    if not merge_request:
        return "not a merge request pipeline; no comment posted"
    token = env.get(token_env)
    if not token:
        return f"{token_env} is not set; no comment posted (see the GitLab CI docs to enable comments)"
    with open(report_path, encoding="utf-8") as handle:
        body = handle.read()
    outcome = post_report(body, api_url=env["CI_API_V4_URL"], project_id=env["CI_PROJECT_ID"],
                          merge_request=merge_request, token=token, opener=opener)
    return f"{outcome} the Marshal comment on merge request !{merge_request}"


__all__ = ["GitLabError", "post_from_pipeline", "post_report"]
