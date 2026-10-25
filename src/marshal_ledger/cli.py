"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

import yaml

from . import __version__
from .diff import diff_specs, format_changes
from .model import AgentSpec, DeploymentEvent
from .readers import READERS, ReadOptions, ReadResult, detect
from .store import Store, StoreError

DEFAULT_DB = "marshal.sqlite"


def _fail(message: str, code: int = 1) -> int:
    print(f"marshal: {message}", file=sys.stderr)
    return code


def _run_reader(args: argparse.Namespace) -> ReadResult:
    path = Path(args.path)
    reader_cls = READERS[args.format] if args.format else detect(path)
    if reader_cls is None:
        raise ValueError(f"no reader recognizes {path}")
    options = ReadOptions(
        target=args.target,
        hash_only=args.hash_only,
        commit=args.commit,
        pipeline_run=getattr(args, "pipeline_run", None),
    )
    result = reader_cls().read(path, options)
    for item in result.skipped:
        print(f"skipped {item.artifact}:{item.locator}: {item.reason}", file=sys.stderr)
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return result


def _open_store(args: argparse.Namespace) -> Store:
    return Store(args.db)


def _read(args: argparse.Namespace) -> int:
    try:
        result = _run_reader(args)
    except ValueError as exc:
        return _fail(str(exc))
    docs = [r.model_dump(mode="json", by_alias=True, exclude_none=True) for r in result.records]
    if args.output == "json":
        json.dump(docs, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        yaml.safe_dump_all(docs, sys.stdout, sort_keys=False, allow_unicode=True)
    return 0


def _collect(args: argparse.Namespace) -> int:
    environment = args.environment or args.target
    if not environment:
        return _fail("--environment is required when no --target is given", 2)
    try:
        result = _run_reader(args)
    except ValueError as exc:
        return _fail(str(exc))
    if not result.records:
        print("no agents found", file=sys.stderr)
        return 0
    with _open_store(args) as store:
        recorded = store.record(
            result.records,
            entities=result.entities,
            event=DeploymentEvent(args.event),
            environment=environment,
        )
    for item in recorded:
        note = "new version" if item.new_version else "known version"
        print(f"{args.event} {environment}: {item.agent} {item.version.label} ({note})")
    return 0


def _ls(args: argparse.Namespace) -> int:
    with _open_store(args) as store:
        agents = store.agents(platform=args.platform, tool=args.tool, knowledge=args.knowledge)
    if args.output == "json":
        json.dump(
            [
                {
                    "agent": a.id,
                    "platform": a.platform,
                    "display_name": a.display_name,
                    "latest": a.latest.label,
                    "version": a.latest.record.version,
                    "shipped": {env: f"v{seq}" for env, seq in a.shipped.items()},
                    "completeness": a.latest.record.quality.completeness,
                }
                for a in agents
            ],
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        return 0
    rows = [("AGENT", "PLATFORM", "LATEST", "SHIPPED", "COMPLETE")]
    for a in agents:
        shipped = ", ".join(f"{env} v{seq}" for env, seq in a.shipped.items()) or "-"
        rows.append(
            (a.id, a.platform, a.latest.label, shipped, f"{a.latest.record.quality.completeness:.0%}")
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())
    return 0


def _split(ref: str) -> tuple[str, str | None]:
    agent, sep, selector = ref.rpartition("@")
    return (agent, selector) if sep else (ref, None)


def _diff(args: argparse.Namespace) -> int:
    agent, selector = _split(args.old)
    with _open_store(args) as store:
        try:
            if args.new is None:
                # One argument: what is proposed (or latest) against what is shipped.
                new = store.resolve(agent, selector or "latest")
                base_selector = f"shipped:{args.environment}" if args.environment else "shipped"
                try:
                    old = store.resolve(agent, base_selector)
                    old_spec, old_label = old.record.spec, old.label
                except StoreError:
                    old_spec, old_label = AgentSpec(), "(nothing shipped)"
            else:
                new_agent, new_selector = _split(args.new)
                old = store.resolve(agent, selector or "latest")
                new = store.resolve(new_agent, new_selector or "latest")
                old_spec, old_label = old.record.spec, old.label
                if new_agent != agent:
                    old_label = f"{agent}@{old.label}"
        except StoreError as exc:
            return _fail(str(exc))
    changes = diff_specs(old_spec, new.record.spec)
    if args.output == "json":
        json.dump(
            {
                "agent": new.record.agent,
                "from": old_label,
                "to": new.label,
                "changes": [c.to_dict() for c in changes],
            },
            sys.stdout,
            indent=2,
            default=str,
        )
        sys.stdout.write("\n")
    else:
        header = f"{new.record.agent} ({new.record.platform})  {old_label} → {new.label}"
        print(format_changes(header, changes))
    return 0


def _log(args: argparse.Namespace) -> int:
    with _open_store(args) as store:
        try:
            history = store.history(args.agent)
        except StoreError as exc:
            return _fail(str(exc))
    for event, seq in history:
        when = event.at.strftime("%Y-%m-%d %H:%M:%SZ")
        extra = " ".join(filter(None, [event.commit and f"commit {event.commit}", event.pipeline_run]))
        print(f"{when}  {event.event.value:<8}  {event.environment:<12}  v{seq}  {extra}".rstrip())
    return 0


def _add_read_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("path", help="artifact file or directory")
    parser.add_argument("--format", choices=sorted(READERS), help="source format (detected by default)")
    parser.add_argument("--target", help="apply a deployment target's overrides")
    parser.add_argument("--hash-only", action="store_true", help="omit instruction text")
    parser.add_argument("--commit", help="source commit to record")


def _add_db_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        default=os.environ.get("MARSHAL_DB", DEFAULT_DB),
        help=f"local store file (default: $MARSHAL_DB or {DEFAULT_DB})",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="marshal", description="Marshal — an agent ledger.")
    parser.add_argument("--version", action="version", version=f"marshal {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    read = commands.add_parser("read", help="read agent definitions and print ledger records")
    _add_read_args(read)
    read.add_argument("--output", choices=["yaml", "json"], default="yaml")
    read.set_defaults(handler=_read)

    collect = commands.add_parser("collect", help="read agent definitions and record an event in the store")
    _add_read_args(collect)
    collect.add_argument("--event", choices=[e.value for e in DeploymentEvent], required=True)
    collect.add_argument("--environment", help="environment the event applies to (default: --target)")
    collect.add_argument("--pipeline-run", help="pipeline run identifier to record")
    _add_db_arg(collect)
    collect.set_defaults(handler=_collect)

    ls = commands.add_parser("ls", help="list agents in the store")
    ls.add_argument("--platform")
    ls.add_argument("--tool", help="only agents whose latest version uses a matching tool (glob)")
    ls.add_argument("--knowledge", help="only agents whose latest version uses matching knowledge (glob)")
    ls.add_argument("--output", choices=["table", "json"], default="table")
    _add_db_arg(ls)
    ls.set_defaults(handler=_ls)

    diff = commands.add_parser(
        "diff",
        help="compare two versions of an agent",
        description="Compare AGENT[@SELECTOR] with another version. With one argument, compare it "
        "against the last shipped version. Selectors: vN, a hash prefix, latest, shipped[:ENV], "
        "proposed[:ENV].",
    )
    diff.add_argument("old", metavar="AGENT[@SELECTOR]")
    diff.add_argument("new", metavar="AGENT[@SELECTOR]", nargs="?")
    diff.add_argument("--environment", help="with one argument: compare against what is shipped here")
    diff.add_argument("--output", choices=["text", "json"], default="text")
    _add_db_arg(diff)
    diff.set_defaults(handler=_diff)

    log = commands.add_parser("log", help="show an agent's proposed and shipped events")
    log.add_argument("agent")
    _add_db_arg(log)
    log.set_defaults(handler=_log)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
