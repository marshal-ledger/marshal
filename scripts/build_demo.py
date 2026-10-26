"""Build a demo ledger from real, public agent definitions.

Fetches the golden fixtures if needed, records them in a store with a short
history (agents shipped to prod, a proposed change that adds a write tool, and
a Copilot Studio agent that drifted after an edit in its web UI), then prints
how to browse it.

    python scripts/build_demo.py [demo.sqlite]
    marshal web --db demo.sqlite
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from marshal_ledger.model import Channel, DeploymentEvent
from marshal_ledger.readers import READERS, ReadOptions, detect
from marshal_ledger.service import ingest
from marshal_ledger.store import Store

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "tests" / "fixtures" / ".cache"
SOURCES = [
    ("databricks/agent-openai-agents-sdk", "prod"),
    ("databricks/agent-langgraph", "prod"),
    ("databricks/genie-space-nyc-taxi", None),
    ("cloudformation/bedrock-agents", None),
    ("cloudformation/agentcore", None),
    ("copilot-studio/all-the-things", None),
    ("copilot-studio/mcp-agent", None),
    ("agentforce/agent-script/order-service.agent", None),
    ("salesforce-metadata/coral-cloud", None),
    ("python/google-adk", None),
    ("adk-config/samples/multi_agent", None),
    ("adl/examples/packages", None),
]


def main() -> int:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "demo.sqlite")
    if not CACHE.exists():
        subprocess.run([sys.executable, str(ROOT / "scripts" / "fetch_fixtures.py")], check=True)
    if target.exists():
        target.unlink()
    start = datetime.now(timezone.utc) - timedelta(days=45)
    with Store(target) as store:
        for fixture, bundle_target in SOURCES:
            path = CACHE / fixture
            reader = detect(path)
            if reader is None:
                print(f"skipped {fixture}: not fetched", file=sys.stderr)
                continue
            result = reader().read(path, ReadOptions(target=bundle_target))
            ingest(store, result.records, entities=result.entities, event=DeploymentEvent.SHIPPED,
                   environment="prod", at=start, channel=Channel.CI)
            print(f"{fixture}: {len(result.records)} agent(s)")

        # A proposed change that gives an agent a new write tool.
        with tempfile.TemporaryDirectory() as work:
            agent = Path(work) / "agent"
            shutil.copytree(CACHE / "databricks/agent-openai-agents-sdk", agent)
            bundle = agent / "databricks.yml"
            bundle.write_text(bundle.read_text().replace(
                "        - name: 'llm-endpoint'",
                "        - name: 'refunds'\n          uc_securable:\n            securable_full_name: 'main.billing.issue_refund'\n"
                "            securable_type: 'FUNCTION'\n            permission: 'EXECUTE'\n        - name: 'llm-endpoint'",
            ))
            result = READERS["databricks-asset-bundle"]().read(agent, ReadOptions(target="prod"))
            ingest(store, result.records, event=DeploymentEvent.PROPOSED, environment="prod",
                   at=start + timedelta(days=40), channel=Channel.CI)

            # A Copilot Studio agent whose tool was switched to the maker's credentials in the web UI.
            edited = Path(work) / "copilot"
            shutil.copytree(CACHE / "copilot-studio/mcp-agent", edited)
            action = edited / "actions" / "SearchDocs.mcs.yml"
            action.write_text(action.read_text().replace("mode: Invoker", "mode: Maker"))
            result = READERS["copilot-studio"]().read(edited)
            ingest(store, result.records, event=DeploymentEvent.OBSERVED, environment="prod",
                   at=start + timedelta(days=44), channel=Channel.SCHEDULED)
    print(f"\nDemo ledger: {target}\nBrowse it:   marshal web --db {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
