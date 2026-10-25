"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

import yaml

from . import __version__
from .readers import READERS, ReadOptions, detect


def _read(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if args.format:
        reader_cls = READERS[args.format]
    else:
        reader_cls = detect(path)
        if reader_cls is None:
            print(f"marshal: no reader recognizes {path}", file=sys.stderr)
            return 2
    options = ReadOptions(target=args.target, hash_only=args.hash_only, commit=args.commit)
    try:
        result = reader_cls().read(path, options)
    except ValueError as exc:
        print(f"marshal: {exc}", file=sys.stderr)
        return 1

    docs = [r.model_dump(mode="json", by_alias=True, exclude_none=True) for r in result.records]
    if args.output == "json":
        json.dump(docs, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        yaml.safe_dump_all(docs, sys.stdout, sort_keys=False, allow_unicode=True)

    for item in result.skipped:
        print(f"skipped {item.artifact}:{item.locator}: {item.reason}", file=sys.stderr)
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="marshal", description="Marshal — an agent ledger.")
    parser.add_argument("--version", action="version", version=f"marshal {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    read = commands.add_parser("read", help="read agent definitions and print ledger records")
    read.add_argument("path", help="artifact file or directory")
    read.add_argument("--format", choices=sorted(READERS), help="source format (detected by default)")
    read.add_argument("--target", help="apply a deployment target's overrides")
    read.add_argument("--hash-only", action="store_true", help="omit instruction text")
    read.add_argument("--commit", help="source commit to record")
    read.add_argument("--output", choices=["yaml", "json"], default="yaml")
    read.set_defaults(handler=_read)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
