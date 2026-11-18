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
import time
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
                dest.write_bytes(download(url))
            except urllib.error.HTTPError as exc:
                if not args.ref or exc.code not in (404, 410):
                    raise
                # At a moving ref, a file can be renamed or removed upstream; report it rather than stop.
                # A server error is not a removal, so it stops the run instead of failing the golden tests.
                missing.append(f"{fixture['id']}: {path} ({exc.code})")
        print(f"fetched {fixture['id']} ({len(fixture['files'])} files)")
    for item in missing:
        print(f"missing at {args.ref}: {item}", file=sys.stderr)
    return 0


def download(url: str, headers: dict[str, str] | None = None, timeout: int = 30, attempts: int = 4) -> bytes:
    """Read a URL, retrying what a later try can fix: timeouts, dropped connections, 429 and 5xx.

    Other HTTP errors, such as a 404 for a file removed upstream, are raised at once.
    """
    request = urllib.request.Request(url, headers=headers or {})
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code != 429 and exc.code < 500 or attempt == attempts:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == attempts:
                raise
        time.sleep(2 * attempt)
    raise AssertionError("unreachable")

if __name__ == "__main__":
    sys.exit(main())
