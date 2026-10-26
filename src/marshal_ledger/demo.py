"""A demo ledger built from real, public agent definitions.

``marshal demo`` downloads a fixed set of agent definitions from public GitHub
repositories, each pinned to a commit, and records them with a short history:
agents shipped to prod, a proposed change that adds a write tool, and a Copilot
Studio agent that drifted after an edit in its web designer.

The definitions are listed in ``demo-sources.json``, a subset of the golden test
fixtures. They are downloaded rather than packaged so each file stays under its
upstream license.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path
from typing import Callable, Optional

from .model import Channel, DeploymentEvent
from .readers import READERS, ReadOptions, detect
from .service import ingest
from .store import Store

PROPOSED = "databricks/agent-openai-agents-sdk"
DRIFTED = "copilot-studio/mcp-agent"


class DemoError(Exception):
    pass


@dataclass(frozen=True)
class DemoSource:
    fixture: str
    """Fixture id, which is also its directory in the cache, as in ``tests/fixtures/sources.json``."""
    read: str
    """What to read, relative to the fixture directory; empty for the whole fixture."""
    target: Optional[str]
    """Deployment target to read it with, such as a Databricks bundle target."""
    repo: str
    commit: str
    root: str
    files: tuple[str, ...]
    """Files to download, relative to ``root`` in the repository and to the fixture directory in the cache."""

    @property
    def path(self) -> str:
        return f"{self.fixture}/{self.read}" if self.read else self.fixture

    def urls(self) -> dict[str, str]:
        prefix = f"{self.root}/" if self.root else ""
        return {name: f"https://raw.githubusercontent.com/{self.repo}/{self.commit}/{prefix}{name}" for name in self.files}


def sources() -> list[DemoSource]:
    document = json.loads(resources.files(__package__).joinpath("demo-sources.json").read_text())
    return [DemoSource(fixture=s["fixture"], read=s.get("read", ""), target=s.get("target"), repo=s["repo"],
                       commit=s["commit"], root=s["root"], files=tuple(s["files"])) for s in document["sources"]]


def fetch(cache: Path, log: Callable[[str], None] = print) -> None:
    """Download any demo definition missing from ``cache``."""
    jobs = []
    for source in sources():
        base = cache / source.fixture
        jobs += [(url, base / name) for name, url in source.urls().items() if not (base / name).is_file()]
    if not jobs:
        return
    log(f"Downloading {len(jobs)} files of public agent definitions from GitHub")

    def get(job: tuple[str, Path]) -> None:
        url, dest = job
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                data = response.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            raise DemoError(f"couldn't download {url}: {exc}") from exc
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(get, jobs))


def build(target: Path, cache: Optional[Path] = None, log: Callable[[str], None] = print) -> int:
    """Record the demo agents in a new store at ``target``. Returns the number of agents.

    ``cache`` holds the downloaded definitions; missing files are fetched into it.
    Without one, they are downloaded to a temporary directory and removed afterwards.
    """
    if cache is None:
        with tempfile.TemporaryDirectory() as work:
            return build(target, Path(work), log)
    fetch(cache, log)
    if target.exists():
        target.unlink()
    start = datetime.now(timezone.utc) - timedelta(days=45)
    with Store(target) as store:
        for source in sources():
            path = cache / source.path
            reader = detect(path)
            if reader is None:
                raise DemoError(f"no reader recognizes {path}")
            result = reader().read(path, ReadOptions(target=source.target))
            ingest(store, result.records, entities=result.entities, event=DeploymentEvent.SHIPPED,
                   environment="prod", at=start, channel=Channel.CI)
            log(f"{source.path}: {len(result.records)} agent{'s' * (len(result.records) != 1)}")

        with tempfile.TemporaryDirectory() as work:
            # A proposed change that gives an agent a new write tool.
            agent = Path(work) / "agent"
            shutil.copytree(cache / PROPOSED, agent)
            bundle = agent / "databricks.yml"
            bundle.write_text(bundle.read_text().replace(
                "        - name: 'llm-endpoint'",
                "        - name: 'refunds'\n          uc_securable:\n            securable_full_name: 'main.billing.issue_refund'\n"
                "            securable_type: 'FUNCTION'\n            permission: 'EXECUTE'\n        - name: 'llm-endpoint'",
            ))
            result = READERS["databricks-asset-bundle"]().read(agent, ReadOptions(target="prod"))
            ingest(store, result.records, event=DeploymentEvent.PROPOSED, environment="prod",
                   at=start + timedelta(days=40), channel=Channel.CI)

            # A Copilot Studio agent whose tool was switched to the maker's credentials in the web designer.
            edited = Path(work) / "copilot"
            shutil.copytree(cache / DRIFTED, edited)
            action = edited / "actions" / "SearchDocs.mcs.yml"
            action.write_text(action.read_text().replace("mode: Invoker", "mode: Maker"))
            result = READERS["copilot-studio"]().read(edited)
            ingest(store, result.records, event=DeploymentEvent.OBSERVED, environment="prod",
                   at=start + timedelta(days=44), channel=Channel.SCHEDULED)
        return len(store.agents())


__all__ = ["DemoError", "DemoSource", "build", "fetch", "sources"]
