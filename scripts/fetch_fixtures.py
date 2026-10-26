"""Download the golden fixtures listed in tests/fixtures/sources.json.

Each fixture is a set of files from a public repository, pinned to a commit.
Files are written to tests/fixtures/.cache/<fixture id>/, which is not committed.
Golden tests skip any fixture that has not been fetched.

    python scripts/fetch_fixtures.py                          # the pinned commits
    python scripts/fetch_fixtures.py --ref HEAD --cache DIR   # each source's latest commit, for upstream checks

Golden tests read fixtures from MARSHAL_FIXTURE_CACHE when it is set.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "tests" / "fixtures" / "sources.json"
CACHE = ROOT / "tests" / "fixtures" / ".cache"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download the golden fixtures.")
    parser.add_argument("--ref", help="fetch at this ref (such as HEAD) instead of each fixture's pinned commit")
    parser.add_argument("--cache", type=Path, default=CACHE, help="where to write the fixtures")
    args = parser.parse_args(argv)
    fixtures = json.loads(SOURCES.read_text())["fixtures"]
    missing = []
    for fixture in fixtures:
        target = args.cache / fixture["id"]
        ref = args.ref or fixture["commit"]
        for name in fixture["files"]:
            path = f"{fixture['root']}/{name}" if fixture["root"] else name
            url = f"https://raw.githubusercontent.com/{fixture['repo']}/{ref}/{path}"
            dest = target / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                with urllib.request.urlopen(url, timeout=30) as response:
                    dest.write_bytes(response.read())
            except urllib.error.HTTPError as exc:
                if not args.ref:
                    raise
                # At a moving ref, a file can be renamed or removed upstream; report it rather than stop.
                missing.append(f"{fixture['id']}: {path} ({exc.code})")
        print(f"fetched {fixture['id']} ({len(fixture['files'])} files)")
    for item in missing:
        print(f"missing at {args.ref}: {item}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
