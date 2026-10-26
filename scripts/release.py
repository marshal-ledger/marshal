"""Release checks and notes, used by the release workflow.

    python scripts/release.py check v0.1.0   # the tag matches every version number, and the changelog dates it
    python scripts/release.py notes v0.1.0   # print that version's changelog section

The package version lives in src/marshal_ledger/__init__.py. The Helm chart's
appVersion must match it; the chart's own version moves independently.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
HEADING = re.compile(r"^## (?P<version>\S+) \((?P<date>[^)]*)\)\s*$", re.MULTILINE)


def package_version() -> str:
    text = (ROOT / "src" / "marshal_ledger" / "__init__.py").read_text()
    return re.search(r'^__version__ = "([^"]+)"', text, re.MULTILINE).group(1)


def chart_app_version() -> str:
    return str(yaml.safe_load((ROOT / "charts" / "marshal" / "Chart.yaml").read_text())["appVersion"])


def changelog_section(version: str, changelog: str | None = None) -> tuple[str, str]:
    """The date and body of a version's changelog section."""
    text = changelog if changelog is not None else (ROOT / "CHANGELOG.md").read_text()
    headings = list(HEADING.finditer(text))
    for i, match in enumerate(headings):
        if match["version"] == version:
            end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
            return match["date"], text[match.end():end].strip()
    raise ValueError(f"CHANGELOG.md has no section for {version}")


def check(tag: str) -> list[str]:
    problems = []
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        problems.append(f"{tag} isn't a release tag such as v1.2.3")
    version = tag.removeprefix("v")
    if package_version() != version:
        problems.append(f"__version__ is {package_version()}, not {version}")
    if chart_app_version() != version:
        problems.append(f"charts/marshal/Chart.yaml appVersion is {chart_app_version()}, not {version}")
    try:
        date, _ = changelog_section(version)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            problems.append(f"CHANGELOG.md section {version} is dated '{date}', not a release date")
    except ValueError as exc:
        problems.append(str(exc))
    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in ("check", "notes"):
        print(__doc__, file=sys.stderr)
        return 2
    command, tag = argv
    if command == "notes":
        print(changelog_section(tag.removeprefix("v"))[1])
        return 0
    problems = check(tag)
    for problem in problems:
        print(f"release: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
