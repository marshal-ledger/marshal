"""Version numbers, the changelog and the docs agree, and the release script reads them."""

import importlib.util
import re
from pathlib import Path

import pytest

from marshal_ledger import __version__

ROOT = Path(__file__).resolve().parents[2]
RELEASE = re.sub(r"\.dev\d+$", "", __version__)  # the release being prepared, or the one just made


def release_script():
    spec = importlib.util.spec_from_file_location("release", ROOT / "scripts" / "release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def latest_published() -> str:
    """The release users get today: this one, or between releases the newest dated changelog section."""
    if __version__ == RELEASE:
        return RELEASE
    dated = [m["version"] for m in release_script().HEADING.finditer((ROOT / "CHANGELOG.md").read_text())
             if re.fullmatch(r"\d{4}-\d{2}-\d{2}", m["date"])]
    return dated[0] if dated else RELEASE


PUBLISHED = latest_published()
MAJOR = f"v{PUBLISHED.split('.')[0]}"


def test_chart_app_version_is_the_published_release():
    """The chart's default image tag is its appVersion, so it names a release that has an image."""
    assert release_script().chart_app_version() == PUBLISHED


def test_changelog_has_a_section_for_the_version():
    date, body = release_script().changelog_section(RELEASE)
    if __version__ != RELEASE:
        assert date == "unreleased"
    else:
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) and body, "a release has a dated section listing its changes"


def test_action_references_use_the_current_major_tag():
    for path in [*ROOT.glob("docs/**/*.md"), *ROOT.glob("examples/**/*.yml"), ROOT / "README.md", ROOT / "collect" / "gitlab-ci.yml"]:
        text = path.read_text()
        for tag in re.findall(r"marshal-ledger/marshal/collect@(\S+)", text):
            assert tag == MAJOR, f"{path.relative_to(ROOT)}: collect@{tag}, expected @{MAJOR}"
        # GitLab includes the template from a tag the same way.
        for tag in re.findall(r"marshal-ledger/marshal/(v[\w.]+)/collect/gitlab-ci\.yml", text):
            assert tag == MAJOR, f"{path.relative_to(ROOT)}: GitLab template from {tag}, expected {MAJOR}"


@pytest.mark.parametrize("doc", ["docs/deployment.md", "docs/github-action.md"])
def test_versions_in_docs_are_the_published_release(doc):
    # Marshal's own versions: image tags, release tags and Action refs (not, say, a runner version).
    versions = set(re.findall(r"(?:marshal:|@v|checkout v)(\d+\.\d+\.\d+)\b", (ROOT / doc).read_text()))
    assert versions, f"{doc} no longer names a release; update this test"
    assert versions == {PUBLISHED}, f"{doc} mentions {sorted(versions)}; update it to {PUBLISHED}"


def test_release_check_and_notes(monkeypatch):
    release = release_script()
    changelog = "# Changelog\n\n## 0.2.0 (unreleased)\n\nNext.\n\n## 0.1.0 (2026-11-02)\n\nFirst.\n"
    assert release.changelog_section("0.1.0", changelog) == ("2026-11-02", "First.")
    assert release.changelog_section("0.2.0", changelog) == ("unreleased", "Next.")
    with pytest.raises(ValueError):
        release.changelog_section("9.9.9", changelog)

    monkeypatch.setattr(release, "package_version", lambda: "0.1.0")
    monkeypatch.setattr(release, "chart_app_version", lambda: "0.1.0")
    monkeypatch.setattr(release, "changelog_section", lambda version: ("2026-11-02", "First."))
    assert release.check("v0.1.0") == []
    assert any("not 0.2.0" in p for p in release.check("v0.2.0"))
    assert release.check("0.1.0") and release.check("v0.1.0-rc1")
    monkeypatch.setattr(release, "changelog_section", lambda version: ("unreleased", "First."))
    assert any("release date" in p for p in release.check("v0.1.0"))
