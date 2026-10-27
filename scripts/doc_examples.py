"""Regenerate the example outputs shown in the documentation.

Each example runs real Marshal commands against the demo store (``marshal demo``) or a
public fixture, so no output in the docs is written by hand. In a Markdown file, an
example is the fenced block right after a ``<!-- example: NAME -->`` comment.

    python scripts/fetch_fixtures.py
    python scripts/doc_examples.py           # rewrite the blocks in docs/ and README.md
    python scripts/doc_examples.py --check   # exit 1 if any block is out of date

tests/golden/test_doc_examples.py runs the check.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Callable

import yaml

from marshal_ledger import demo
from marshal_ledger.cli import main as marshal
from marshal_ledger.readers import READERS

ROOT = Path(__file__).resolve().parents[1]
CACHE = Path(os.environ.get("MARSHAL_FIXTURE_CACHE") or ROOT / "tests" / "fixtures" / ".cache")
PAGES = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
MARKER = re.compile(r"(<!-- example: ([\w-]+) -->\n```[^\n]*\n)(.*?)(\n```)", re.S)


def run(*args: str, stream: str = "stdout") -> str:
    """Run a marshal command in this process and return what it printed."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        marshal([str(a) for a in args])
    return (out if stream == "stdout" else err).getvalue().rstrip("\n")


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, f"{old!r} not in {path}"
    path.write_text(text.replace(old, new, 1))


class Examples:
    """Example outputs, by name. Each method gets a fresh copy of the demo store when it needs one."""

    def __init__(self, work: Path):
        self.work = work
        self.demo = work / "demo.sqlite"
        demo.build(self.demo, CACHE, log=lambda _: None)

    def store(self, name: str) -> Path:
        copy = self.work / f"{name}.sqlite"
        shutil.copy(self.demo, copy)
        return copy

    def fixture(self, name: str, source: str) -> Path:
        copy = self.work / name / Path(source).name
        shutil.copytree(CACHE / source, copy)
        return copy

    def deps_across_platforms(self) -> str:
        return run("deps", "*api.acme.example/invoices*", "--db", self.demo)

    def diff_refund(self) -> str:
        return run("diff", "agent_openai_agents_sdk/agent_openai_agents_sdk", "--db", self.demo)

    def diff_drift(self) -> str:
        return run("diff", "eval_testAgent", "--db", self.demo)

    def port_refund(self) -> str:
        return run("port", "agent_openai_agents_sdk/agent_openai_agents_sdk", "--to", "copilot-studio", "--db", self.demo)

    def port_billing(self) -> str:
        return run("port", "cost-explorer-agent/agent_template/BillingAgent", "--to", "copilot-studio", "--db", self.demo)

    def convert_invoice(self) -> str:
        agent = CACHE / "adl/examples/packages/adl-agent/examples/openclaw-passport/enterprise/agents/invoice-processor.adl.yaml"
        return run("convert", agent, "--to", "agent-spec", stream="stderr")

    def diff_guardrail(self) -> str:
        """The public ADL invoice processor, with the guardrail sentence taken out of its instructions."""
        source = CACHE / "adl/examples/packages/adl-agent/examples/openclaw-passport/enterprise/agents/invoice-processor.adl.yaml"
        agent = self.work / "guardrail" / source.name
        agent.parent.mkdir()
        shutil.copy(source, agent)
        db = self.work / "guardrail.sqlite"
        run("collect", agent, "--db", db, "--event", "shipped", "--environment", "prod")
        edit(agent, "Match invoices to purchase orders,\n  flag discrepancies, and never approve payment without explicit AP approval.",
             "Match invoices to purchase orders\n  and flag discrepancies.")
        run("collect", agent, "--db", db, "--event", "proposed", "--environment", "prod")
        return run("diff", "https://agents.acme.example/invoice-processor", "--db", db)

    def record_hr(self) -> str:
        """An abbreviated ledger record: the public Bedrock HR assistant, as `marshal read` prints it."""
        path = CACHE / "cloudformation/bedrock-agents/hr-assistant"
        record = READERS["cloudformation"]().read(path).records[0].model_dump(mode="json", by_alias=True, exclude_none=True)
        spec = record["spec"]
        spec["instructions"]["text"] = spec["instructions"]["text"][:60] + "…"
        spec["tools"] = spec["tools"][:1]
        spec["permissions"]["grants"] = spec["permissions"]["grants"][:2]
        excerpt = {key: record[key] for key in ("apiVersion", "kind", "agent", "platform", "version", "source")}
        excerpt["spec"] = {key: spec[key] for key in ("model", "instructions", "tools", "permissions")}
        excerpt["quality"] = {"completeness": record["quality"]["completeness"]}
        dumped = yaml.safe_dump(excerpt, sort_keys=False, width=100, allow_unicode=True).rstrip("\n")
        return dumped + "\n# Abbreviated: three more tools, two more grants, the Bedrock extension block and the unknown fields."

    def pr_comment_maker_credentials(self) -> str:
        """The demo's Copilot Studio edit, proposed in a pull request instead of made in the web designer."""
        agent = self.fixture("maker", demo.DRIFTED)
        edit(agent / "actions" / "SearchDocs.mcs.yml", "mode: Invoker", "mode: Maker")
        report = self.work / "maker.md"
        run("collect", agent, "--db", self.store("maker"), "--event", "proposed", "--environment", "prod",
            "--report", report)
        return comment(report)

    def pr_comment_dynamodb(self) -> str:
        """The public Bedrock HR assistant, with one action added to its agent's role."""
        agent = self.fixture("hr", "cloudformation/bedrock-agents/hr-assistant")
        template = agent / "cfn" / "hr-resources.yml"
        db = self.work / "hr.sqlite"
        run("collect", agent, "--db", db, "--event", "shipped", "--environment", "prod")
        edit(template, "              - bedrock:Retrieve\n              - s3:GetObject\n",
             "              - bedrock:Retrieve\n              - s3:GetObject\n              - dynamodb:UpdateItem\n")
        report = self.work / "hr.md"
        run("collect", agent, "--db", db, "--event", "proposed", "--environment", "prod", "--report", report)
        return comment(report)


def comment(report: Path) -> str:
    """The body of a pull request comment, without its marker and heading."""
    lines = report.read_text().splitlines()[3:]
    return "\n".join(line for line in lines if line != "```").strip("\n")


def generators(examples: Examples) -> dict[str, Callable[[], str]]:
    return {
        name.replace("_", "-"): getattr(examples, name)
        for name in dir(examples)
        if not name.startswith("_") and name not in ("store", "fixture", "work", "demo")
    }


def update(check: bool) -> list[str]:
    """Rewrite (or, with ``check``, compare) every example block. Returns the stale ones."""
    stale, missing = [], []
    with tempfile.TemporaryDirectory() as work:
        known = generators(Examples(Path(work)))
        outputs: dict[str, str] = {}
        for page in PAGES:
            text = page.read_text()

            def replace(match: re.Match) -> str:
                name = match.group(2)
                if name not in known:
                    missing.append(f"{page.relative_to(ROOT)}: {name}")
                    return match.group(0)
                if name not in outputs:
                    outputs[name] = known[name]()
                if outputs[name] != match.group(3):
                    stale.append(f"{page.relative_to(ROOT)}: {name}")
                return match.group(1) + outputs[name] + match.group(4)

            new = MARKER.sub(replace, text)
            if not check and new != text:
                page.write_text(new)
    if missing:
        raise SystemExit("unknown examples: " + ", ".join(missing))
    return stale


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="report out-of-date examples without changing files")
    args = parser.parse_args()
    stale = update(args.check)
    for name in stale:
        print(("out of date: " if args.check else "updated: ") + name)
    return 1 if args.check and stale else 0


if __name__ == "__main__":
    sys.exit(main())
