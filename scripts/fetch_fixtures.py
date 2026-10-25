"""Download the golden fixtures listed in tests/fixtures/sources.json.

Each fixture is a set of files from a public repository, pinned to a commit.
Files are written to tests/fixtures/.cache/<fixture id>/, which is not committed.
Golden tests skip any fixture that has not been fetched.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "tests" / "fixtures" / "sources.json"
CACHE = ROOT / "tests" / "fixtures" / ".cache"


def main() -> int:
    fixtures = json.loads(SOURCES.read_text())["fixtures"]
    for fixture in fixtures:
        target = CACHE / fixture["id"]
        for name in fixture["files"]:
            path = f"{fixture['root']}/{name}" if fixture["root"] else name
            url = f"https://raw.githubusercontent.com/{fixture['repo']}/{fixture['commit']}/{path}"
            dest = target / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(url, timeout=30) as response:
                dest.write_bytes(response.read())
        print(f"fetched {fixture['id']} ({len(fixture['files'])} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
