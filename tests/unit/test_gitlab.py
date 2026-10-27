"""GitLab CI: the template is valid GitLab CI, and the report is posted as one merge request comment."""

import io
import json
import os
import re
from pathlib import Path

import jsonschema
import pytest
import yaml
from jsonschema.exceptions import best_match

from marshal_ledger import __version__
from marshal_ledger.gitlab import GitLabError, post_from_pipeline, post_report
from marshal_ledger.report import COMMENT_MARKER

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "collect" / "gitlab-ci.yml"
SCHEMA = Path(os.environ.get("MARSHAL_FIXTURE_CACHE") or ROOT / "tests" / "fixtures" / ".cache") / "gitlab" / "schema" / "ci.json"


class FakeGitLab:
    """A merge request's notes, served the way GitLab's API does."""

    def __init__(self, notes):
        self.notes = notes
        self.calls = []

    def __call__(self, request):
        method, url = request.get_method(), request.full_url
        self.calls.append((method, url, request.get_header("Authorization")))
        body = json.loads(request.data) if request.data else None
        if method == "GET":
            page = int(re.search(r"[?&]page=(\d+)", url).group(1))
            result = self.notes[(page - 1) * 100: page * 100]
        elif method == "PUT":
            note_id = int(url.rsplit("/", 1)[1])
            next(n for n in self.notes if n["id"] == note_id)["body"] = body["body"]
            result = {}
        else:
            self.notes.append({"id": len(self.notes) + 1, "body": body["body"], "system": False})
            result = {}
        return io.BytesIO(json.dumps(result).encode())


API = "https://gitlab.example.com/api/v4"


def test_a_report_is_posted_once_then_updated():
    gitlab = FakeGitLab([{"id": 1, "body": "Looks good", "system": False}])
    report = f"{COMMENT_MARKER}\n### Marshal: agent changes"
    assert post_report(report, api_url=API, project_id="42", merge_request="7", token="t0ken", opener=gitlab) == "created"
    assert post_report(report + " for `prod`", api_url=API, project_id="42", merge_request="7", token="t0ken",
                       opener=gitlab) == "updated"
    assert [n["body"] for n in gitlab.notes] == ["Looks good", report + " for `prod`"]
    assert all(call[1].startswith(f"{API}/projects/42/merge_requests/7/notes") for call in gitlab.calls)
    assert {call[2] for call in gitlab.calls} == {"Bearer t0ken"}


def test_the_existing_comment_is_found_on_a_later_page():
    notes = [{"id": i, "body": "chatter", "system": False} for i in range(1, 151)]
    notes[120]["body"] = f"{COMMENT_MARKER}\nold"
    gitlab = FakeGitLab(notes)
    assert post_report(f"{COMMENT_MARKER}\nnew", api_url=API, project_id="42", merge_request="7", token="t",
                       opener=gitlab) == "updated"
    assert notes[120]["body"].endswith("new") and len(notes) == 150


def test_outside_a_merge_request_or_without_a_token_nothing_is_posted(tmp_path):
    report = tmp_path / "report.md"
    report.write_text(COMMENT_MARKER)
    env = {"CI_API_V4_URL": API, "CI_PROJECT_ID": "42"}
    assert "not a merge request pipeline" in post_from_pipeline(str(report), "MARSHAL_GITLAB_TOKEN", env)
    assert "MARSHAL_GITLAB_TOKEN is not set" in post_from_pipeline(str(report), "MARSHAL_GITLAB_TOKEN",
                                                                    env | {"CI_MERGE_REQUEST_IID": "7"})


def test_api_errors_never_show_the_token(tmp_path):
    import urllib.error

    def refuse(request):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

    with pytest.raises(GitLabError) as raised:
        post_report("x", api_url=API, project_id="42", merge_request="7", token="s3cret-token", opener=refuse)
    assert "403" in str(raised.value) and "s3cret-token" not in str(raised.value)


def template():
    return yaml.safe_load(TEMPLATE.read_text())


def test_template_installs_the_release_it_comes_from():
    # Teams include the template from a release tag, so it installs that release from PyPI.
    assert template()[".marshal-collect"]["variables"]["MARSHAL_VERSION"] == re.sub(r"\.dev\d+$", "", __version__)


def test_template_is_valid_gitlab_ci():
    if not SCHEMA.exists():
        pytest.skip("GitLab CI schema not fetched; run python scripts/fetch_fixtures.py")
    validator = jsonschema.Draft7Validator(json.loads(SCHEMA.read_text()))
    pipeline = template() | {
        "marshal-proposed": {"extends": ".marshal-collect", "variables": {"MARSHAL_URL": "https://marshal.example.com"},
                             "rules": [{"if": '$CI_PIPELINE_SOURCE == "merge_request_event"'}]},
    }
    error = best_match(validator.iter_errors(pipeline))
    assert error is None, f"{list(error.absolute_path)}: {error.message[:300]}"


def test_template_records_what_the_github_action_records():
    job = template()[".marshal-collect"]
    script = "\n".join(job["script"])
    for flag in ("--event", "--url", "--token-env MARSHAL_ID_TOKEN", "--commit", "--target", "--environment",
                 "--portability", "--report marshal-report.md"):
        assert flag in script
    assert job["id_tokens"]["MARSHAL_ID_TOKEN"]["aud"] == "marshal"
    assert job["artifacts"]["paths"] == ["marshal-report.md"]


def test_template_can_install_marshal_from_elsewhere():
    job = template()[".marshal-collect"]
    assert job["variables"]["MARSHAL_PACKAGE"] == ""
    assert '"${MARSHAL_PACKAGE:-marshal-ledger==${MARSHAL_VERSION}}"' in job["script"][0]
