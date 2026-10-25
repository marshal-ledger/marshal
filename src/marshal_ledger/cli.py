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
from .diff import diff_specs
from .model import AgentSpec, DeploymentEvent
from .readers import READERS, ReadOptions, ReadResult, detect
from .report import markdown, text
from .risk import rank
from .service import AgentReport, ingest
from .store import Store, StoreError

DEFAULT_DB = "marshal.sqlite"
DEFAULT_AUDIENCE = "marshal"


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


def _emit(reports: list[AgentReport], args: argparse.Namespace) -> None:
    for report in reports:
        note = "new version" if report.new_version else "known version"
        print(f"{report.event} {report.environment}: {report.agent} {report.label} ({note})")
    if reports:
        print()
        print(text(reports))
    if args.report:
        Path(args.report).write_text(markdown(reports), encoding="utf-8")


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
    with _open_store(args) as store:
        reports = ingest(
            store,
            result.records,
            entities=result.entities,
            event=DeploymentEvent(args.event),
            environment=environment,
            hash_only=args.hash_only,
        )
    _emit(reports, args)
    return 0


def _push(args: argparse.Namespace) -> int:
    from .client import ClientError, ci_token, push

    environment = args.environment or args.target
    if not environment:
        return _fail("--environment is required when no --target is given", 2)
    if not args.url:
        return _fail("--url (or $MARSHAL_URL) is required", 2)
    try:
        result = _run_reader(args)
    except ValueError as exc:
        return _fail(str(exc))
    if not result.records:
        print("no agents found", file=sys.stderr)
    try:
        token = ci_token(args.audience, args.token_env)
        reports = push(
            args.url,
            token,
            result.records,
            event=DeploymentEvent(args.event),
            environment=environment,
            entities=result.entities,
        )
    except ClientError as exc:
        return _fail(str(exc))
    _emit(reports, args)
    return 0


def _convert(args: argparse.Namespace) -> int:
    from .convert import ConversionError, convert
    from .writers import WRITERS

    if args.to not in WRITERS:
        return _fail(f"no writer for {args.to!r}; writers: {', '.join(sorted(WRITERS))}", 2)
    try:
        result = _run_reader(args)
    except ValueError as exc:
        return _fail(str(exc))
    if not result.records:
        return _fail("no agents found")
    if len(result.records) > 1 and not args.out:
        return _fail(f"{len(result.records)} agents found; pass --out DIR to write one file per agent", 2)
    if getattr(WRITERS[args.to], "multi_file", False) and not args.out:
        return _fail(f"{args.to} definitions span several files; pass --out DIR", 2)
    try:
        conversions = [convert(record, args.to) for record in result.records]
    except ConversionError as exc:
        return _fail(str(exc))

    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        used: set[str] = set()
        for conversion in conversions:
            name = conversion.filename
            stem, dot, suffix = name.partition(".")
            n = 1
            while name in used:
                n += 1
                name = f"{stem}-{n}{dot}{suffix}"
            used.add(name)
            if conversion.files and getattr(WRITERS[args.to], "multi_file", False):
                for path, text in conversion.files.items():
                    destination = out / stem / path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(text, encoding="utf-8")
                print(f"wrote {out / stem}/", file=sys.stderr)
            else:
                (out / name).write_text(conversion.text, encoding="utf-8")
                print(f"wrote {out / name}", file=sys.stderr)
    else:
        sys.stdout.write(conversions[0].text)

    for conversion in conversions:
        print(conversion.report.text(), file=sys.stderr)
        for warning in conversion.warnings:
            print(f"  warning: {warning}", file=sys.stderr)
    if args.report:
        Path(args.report).write_text(
            json.dumps([c.report.to_dict() for c in conversions], indent=2, default=str) + "\n", encoding="utf-8"
        )
    return 0


def _serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn

        from .server import ServerConfig, create_app
    except ImportError:
        return _fail("the server needs extra packages: pip install 'marshal-ledger[server]'")
    config = ServerConfig.load(args.config)
    uvicorn.run(create_app(config), host=args.host, port=args.port, proxy_headers=True)
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
    changes = rank(diff_specs(old_spec, new.record.spec))
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
        print(f"{new.record.agent} ({new.record.platform})  {old_label} → {new.label}")
        print()
        for change in changes:
            print(f"  {change.line()}")
        if not changes:
            print("  no changes")
        print()
        print(f"  Completeness {new.record.quality.completeness:.0%}")
    return 0


def _log(args: argparse.Namespace) -> int:
    with _open_store(args) as store:
        try:
            history = store.history(args.agent)
        except StoreError as exc:
            return _fail(str(exc))
    for event, seq, actor in history:
        when = event.at.strftime("%Y-%m-%d %H:%M:%SZ")
        extra = " ".join(
            filter(
                None,
                [
                    event.commit and f"commit {event.commit}",
                    event.pipeline_run,
                    actor and f"by {actor.repository}" + (f" ({actor.ref})" if actor.ref else ""),
                ],
            )
        )
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

    def add_event_args(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--event", choices=[e.value for e in DeploymentEvent], required=True)
        parser.add_argument("--environment", help="environment the event applies to (default: --target)")
        parser.add_argument("--pipeline-run", help="pipeline run identifier to record")
        parser.add_argument("--report", metavar="FILE", help="also write a Markdown report, for a PR comment")

    collect = commands.add_parser("collect", help="read agent definitions and record an event in a local store")
    _add_read_args(collect)
    add_event_args(collect)
    _add_db_arg(collect)
    collect.set_defaults(handler=_collect)

    push_ = commands.add_parser("push", help="read agent definitions and send an event to the ingest API")
    _add_read_args(push_)
    add_event_args(push_)
    push_.add_argument("--url", default=os.environ.get("MARSHAL_URL"), help="ingest API base URL ($MARSHAL_URL)")
    push_.add_argument("--audience", default=DEFAULT_AUDIENCE, help="OIDC audience to request (default: marshal)")
    push_.add_argument("--token-env", help="read the OIDC token from this variable instead of GitHub Actions")
    push_.set_defaults(handler=_push)

    from .writers import WRITERS

    convert_ = commands.add_parser(
        "convert",
        help="convert agent definitions to another format, with a fidelity report",
        description="Read agents in any supported format and write them in another. The fidelity report "
        "(on stderr) is computed by reading the written document back.",
    )
    _add_read_args(convert_)
    convert_.add_argument("--to", required=True, choices=sorted(WRITERS), help="target format")
    convert_.add_argument("--out", metavar="DIR", help="write one file per agent here (required for several agents)")
    convert_.add_argument("--report", metavar="FILE", help="also write the fidelity reports as JSON")
    convert_.set_defaults(handler=_convert)

    serve = commands.add_parser("serve", help="run the ingest API")
    serve.add_argument("--config", required=True, help="server configuration file (YAML)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.set_defaults(handler=_serve)

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
