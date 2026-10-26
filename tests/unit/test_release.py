"""Version numbers, the changelog and the docs agree, and the release script reads them."""

import importlib.util
import re
from pathlib import Path

import pytest

from marshal_ledger import __version__

ROOT = Path(__file__).resolve().parents[2]
RELEASE = re.sub(r"\.dev\d+$", "", __version__)  # the release being prepared, or the one just made
MAJOR = f"v{RELEASE.split('.')[0]}"


def release_script():
    spec = importlib.util.spec_from_file_location("release", ROOT / "scripts" / "release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_chart_app_version_is_the_package_version():
    assert release_script().chart_app_version() == __version__


def test_changelog_has_a_section_for_the_version():
    date, body = release_script().changelog_section(RELEASE)
    assert body
    assert date == "unreleased" if __version__ != RELEASE else re.fullmatch(r"\d{4}-\d{2}-\d{2}", date)


def test_action_references_use_the_current_major_tag():
    for path in [*ROOT.glob("docs/**/*.md"), *ROOT.glob("examples/**/*.yml"), ROOT / "README.md"]:
        for tag in re.findall(r"marshal-ledger/marshal/collect@(\S+)", path.read_text()):
            assert tag == MAJOR, f"{path.relative_to(ROOT)}: collect@{tag}, expected @{MAJOR}"


@pytest.mark.parametrize("doc", ["docs/deployment.md", "docs/github-action.md"])
def test_versions_in_docs_are_the_current_release(doc):
    versions = set(re.findall(r"(?<![\w.])v?(\d+\.\d+\.\d+)\b", (ROOT / doc).read_text()))
    assert versions == {RELEASE}, f"{doc} mentions {sorted(versions)}; update it to {RELEASE}"


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
