"""Watch the upstream definitions Marshal's readers and writers depend on.

For each source in upstream/watch.json, extract its current "surface" (the
field, type and keyword names a reader relies on) and compare it with the
snapshot in upstream/snapshots/<id>.json.

    python scripts/check_upstream.py                 # report changes
    python scripts/check_upstream.py --update        # accept the current surfaces as snapshots
    python scripts/check_upstream.py --issues        # also open, update or close GitHub issues (CI)

With --golden-result, the output of the golden tests run against upstream's
latest definitions is reported as its own issue when they fail.

Only public sources are read. Set GITHUB_TOKEN to raise GitHub's rate limit.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import os
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
WATCH = ROOT / "upstream" / "watch.json"
SNAPSHOTS = ROOT / "upstream" / "snapshots"
ISSUE_LABEL = "upstream"
ISSUE_PREFIX = "Upstream change: "

sys.path.insert(0, str(ROOT / "src"))


def _get(url: str, accept: str | None = None) -> bytes:
    headers = {"User-Agent": "marshal-upstream-check"}
    if accept:
        headers["Accept"] = accept
    token = os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
        return response.read()


def _raw(repo: str, path: str, ref: str = "HEAD") -> bytes:
    return _get(f"https://raw.githubusercontent.com/{repo}/{ref}/{path}")


def _listing(repo: str, path: str, ref: str = "HEAD") -> list[dict[str, Any]]:
    return json.loads(_get(f"https://api.github.com/repos/{repo}/contents/{path}?ref={ref}"))


# -- extractors: each returns a sorted list of names ---------------------------


def databricks_bundle(source: dict[str, Any]) -> list[str]:
    schema = json.loads(_raw(source["repo"], source["path"]))

    def get(ref: str) -> Any:
        node: Any = schema
        for part in ref.lstrip("#/").split("/"):
            node = node[part]
        return node

    def resolve(node: Any) -> Any:
        for _ in range(10):
            if isinstance(node, dict) and "oneOf" in node:
                node = node["oneOf"][0]
            elif isinstance(node, dict) and "$ref" in node:
                node = get(node["$ref"])
            else:
                break
        return node

    resources = resolve(schema["$defs"]["github.com"]["databricks"]["cli"]["bundle"]["config.Resources"])
    surface = []
    for rtype, body in resources["properties"].items():
        surface.append(f"resources.{rtype}")
        if rtype in ("apps", "genie_spaces", "model_serving_endpoints", "mcp_services", "vector_search_indexes"):
            item = resolve(resolve(body).get("additionalProperties", body))
            for prop, value in item.get("properties", {}).items():
                surface.append(f"resources.{rtype}.{prop}")
                if rtype == "apps" and prop == "resources":
                    kinds = resolve(resolve(value).get("items", {}))
                    surface += [f"resources.apps.resources.{k}" for k in kinds.get("properties", {})]
    return sorted(surface)


def cloudformation(source: dict[str, Any]) -> list[str]:
    archive = zipfile.ZipFile(io.BytesIO(_get(source["url"])))
    names = [n for n in archive.namelist()
             if n.removesuffix(".json") in source["types"] or any(n.startswith(p) for p in source["prefixes"])]
    surface = []
    for name in sorted(names):
        schema = json.loads(archive.read(name))
        type_name = schema.get("typeName", name)
        surface.append(type_name)
        surface += [f"{type_name}.{p}" for p in schema.get("properties", {})]
        for definition, body in (schema.get("definitions") or {}).items():
            surface += [f"{type_name}#{definition}.{p}" for p in (body.get("properties") or {})]
    return sorted(surface)


def copilot_studio(source: dict[str, Any]) -> list[str]:
    schema = json.loads(_raw(source["repo"], source["path"]))
    surface = []
    for name, body in schema.get("definitions", {}).items():
        if name.endswith("NoKind"):
            continue
        surface.append(name)
        if name in ("GptComponentMetadata", "TaskDialog", "AgentDialog", "AISettings", "ConnectionProperties",
                    "InvokeConnectorTaskAction", "InvokeExternalAgentTaskAction", "InvokeFlowTaskAction"):
            surface += [f"{name}.{p}" for p in (body.get("properties") or {})]
    return sorted(surface)


def agent_script(source: dict[str, Any]) -> list[str]:
    from marshal_ledger.readers.agentforce import parse_tree

    keywords: set[str] = set()

    def walk(nodes, depth, parent):
        for node in nodes:
            text = node.text
            key = text.split(":", 1)[0].split(" ", 1)[0] if ":" in text or text.endswith(":") else text.split(" ", 1)[0]
            if key.isidentifier() and depth <= 3:
                keywords.add(f"{parent}{key}" if depth else key)
            walk(node.children, depth + 1, f"{key}." if depth == 0 else parent)

    for entry in _listing(source["repo"], source["path"]):
        if entry["name"].endswith(".agent"):
            nodes, _ = parse_tree(_get(entry["download_url"]).decode("utf-8"))
            walk(nodes, 0, "")
    return sorted(keywords)


def adl(source: dict[str, Any]) -> list[str]:
    versions = sorted(e["name"] for e in _listing(source["repo"], source["path"]) if e["name"].endswith(".json"))
    latest = json.loads(_raw(source["repo"], f"{source['path']}/{versions[-1]}"))
    surface = [f"version {v.removesuffix('.json')}" for v in versions]
    props = latest.get("properties", {})
    surface += [f"{p}" for p in props]
    for section in ("tools", "resources"):
        items = (props.get(section) or {}).get("items") or {}
        surface += [f"{section}[].{p}" for p in items.get("properties", {})]
    for section in ("model", "permissions"):
        surface += [f"{section}.{p}" for p in (props.get(section) or {}).get("properties", {})]
    return sorted(surface)


def agent_spec(source: dict[str, Any]) -> list[str]:
    files = sorted(e["name"] for e in _listing(source["repo"], source["path"]) if e["name"].endswith(".json"))
    latest = json.loads(_raw(source["repo"], f"{source['path']}/{files[-1]}"))
    surface = [f"version {f.removeprefix('agentspec_json_spec_').removesuffix('.json').replace('_', '.')}" for f in files]
    for name, body in latest.get("$defs", {}).items():
        const = ((body.get("properties") or {}).get("component_type") or {}).get("const")
        if const:
            surface.append(f"component {const}")
            if const in ("Agent", "ServerTool", "RemoteTool", "MCPTool", "MCPToolBox", "OpenAiConfig", "LlmConfig"):
                surface += [f"{const}.{p}" for p in body.get("properties", {}) if p != "component_type"]
    return sorted(set(surface))


def oasf(source: dict[str, Any]) -> list[str]:
    release = json.loads(_get(f"https://api.github.com/repos/{source['repo']}/releases/latest"))["tag_name"]
    record = json.loads(_raw(source["repo"], "schema/objects/record.json", release))
    tree = json.loads(_get(f"https://api.github.com/repos/{source['repo']}/git/trees/{release}?recursive=1"))
    modules = [t["path"].removeprefix("schema/modules/").removesuffix(".json")
               for t in tree["tree"] if t["path"].startswith("schema/modules/") and t["path"].endswith(".json")]
    return sorted([f"release {release}"] + [f"record.{a}" for a in record.get("attributes", {})]
                  + [f"module {m}" for m in modules])


def adk_config(source: dict[str, Any]) -> list[str]:
    surface = []
    for path in source["paths"]:
        tree = ast.parse(_raw(source["repo"], path).decode("utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                surface.append(node.name)
                surface += [f"{node.name}.{s.target.id}" for s in node.body
                            if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)]
    return sorted(surface)


def pypi_minor(source: dict[str, Any]) -> list[str]:
    surface = []
    for package in source["packages"]:
        version = json.loads(_get(f"https://pypi.org/pypi/{package}/json"))["info"]["version"]
        surface.append(f"{package} {'.'.join(version.split('.')[:2])}")
    return sorted(surface)


EXTRACTORS: dict[str, Callable[[dict[str, Any]], list[str]]] = {
    f.__name__: f for f in (databricks_bundle, cloudformation, copilot_studio, agent_script, adl, agent_spec, oasf,
                            adk_config, pypi_minor)
}


# -- issues ------------------------------------------------------------------


def _gh(*args: str, input_text: str | None = None) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True, input=input_text).stdout


def _sync_issue(title: str, body: str | None) -> None:
    """Open or update the issue titled ``title`` with ``body``, or close it when ``body`` is None."""
    issues = json.loads(_gh("issue", "list", "--label", ISSUE_LABEL, "--state", "open", "--json", "number,title",
                            "--limit", "100"))
    existing = next((i for i in issues if i["title"] == title), None)
    if body is None:
        if existing:
            _gh("issue", "close", str(existing["number"]), "--comment", "The snapshot matches upstream again.")
        return
    if existing:
        _gh("issue", "edit", str(existing["number"]), "--body-file", "-", input_text=body)
    else:
        _gh("issue", "create", "--title", title, "--label", ISSUE_LABEL, "--body-file", "-", input_text=body)


def _body(source: dict[str, Any], change: dict[str, Any]) -> str:
    lines = [f"The upstream definition of **{source['name']}** changed since its snapshot.", ""]
    for label in ("added", "removed"):
        if change[label]:
            lines.append(f"**{label.capitalize()}** ({len(change[label])}):")
            lines += [f"- `{item}`" for item in change[label][:200]]
            if len(change[label]) > 200:
                lines.append(f"- and {len(change[label]) - 200} more")
            lines.append("")
    lines += [
        f"Check whether the reader and writer documented in `{source.get('docs', 'docs/formats/')}` need changes, "
        "add a golden fixture for any new construct, then accept the new surface with "
        "`python scripts/check_upstream.py --update` in the same pull request.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--update", action="store_true", help="write current surfaces as the new snapshots")
    parser.add_argument("--issues", action="store_true", help="open, update or close GitHub issues (needs gh)")
    parser.add_argument("--report", metavar="FILE", help="write the changes as JSON")
    parser.add_argument("--golden-result", metavar="FILE", help="pytest output from golden tests on upstream's latest")
    parser.add_argument("--only", action="append", help="check only these source ids")
    args = parser.parse_args(argv)

    sources = json.loads(WATCH.read_text())["sources"]
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {}
    for source in sources:
        if args.only and source["id"] not in args.only:
            continue
        snapshot_file = SNAPSHOTS / f"{source['id']}.json"
        try:
            surface = EXTRACTORS[source["extractor"]](source)
        except Exception as exc:  # report and carry on; one source failing shouldn't hide the others
            report[source["id"]] = {"error": f"{type(exc).__name__}: {exc}"}
            print(f"{source['id']}: could not check: {exc}", file=sys.stderr)
            continue
        known = json.loads(snapshot_file.read_text()) if snapshot_file.exists() else []
        change = {"added": sorted(set(surface) - set(known)), "removed": sorted(set(known) - set(surface))}
        report[source["id"]] = change
        state = "unchanged" if not (change["added"] or change["removed"]) else \
            f"{len(change['added'])} added, {len(change['removed'])} removed"
        print(f"{source['id']}: {state}")
        if args.update:
            snapshot_file.write_text(json.dumps(surface, indent=1) + "\n")
        if args.issues and not args.update:
            changed = change["added"] or change["removed"]
            _sync_issue(ISSUE_PREFIX + source["name"], _body(source, change) if changed else None)

    if args.golden_result:
        output = Path(args.golden_result).read_text(errors="replace")
        failed = " failed" in output or " error" in output
        report["golden-latest"] = {"failed": failed}
        print(f"golden tests on upstream's latest: {'failing' if failed else 'passing'}")
        if args.issues:
            summary = "\n".join(line for line in output.splitlines() if line.startswith(("FAILED", "ERROR")))[:60000]
            body = ("The golden tests fail when the fixtures are fetched from each source's latest commit instead "
                    "of the pinned one, so an upstream definition changed in a way a reader doesn't handle.\n\n"
                    f"```\n{summary}\n```\n\nFix the reader, then move the pinned commits in "
                    "`tests/fixtures/sources.json` forward.") if failed else None
            _sync_issue(ISSUE_PREFIX + "golden tests fail on latest upstream definitions", body)

    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
