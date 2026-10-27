"""Entity resolution: what a ref reaches, independent of the platform that names it.

Refs name their platform, so two agents on different platforms that call the same
API have different refs. A *target* is a platform-neutral description of what a ref
reaches, derived only from what the ref states:

- ``http:POST https://api.acme.example/refunds/{}``: an HTTP operation, from remote
  tools and HTTP request steps (``tool/http.<METHOD>.<url>``);
- ``mcp:https://api.githubcopilot.com/mcp`` or ``mcp:npm:@notionhq/notion-mcp-server``:
  an MCP server, by URL or by the package an ``npx`` or ``uvx`` command runs;
- ``openapi:<spec>#<operationId>``: an OpenAPI operation, from ADL annotations;
- ``url:https://contoso.sharepoint.com/sites/hr``: a knowledge source at a URL;
- ``lambda:arn:aws:lambda:...``, ``lambda:import:<export>`` or ``lambda:function:<name>``:
  the Lambda function that runs a Bedrock action group, from the record's bindings;
- ``host:api.acme.example``: the API host of any of the URLs above, and of network
  grants. Sharing a host says two agents reach the same system, not that they do the
  same thing, so host matches are weaker.

Some refs reach something their definition doesn't name, such as a Power Platform MCP
connector whose server the vendor documents. ``equivalents/*.yaml`` lists those links,
each with the vendor pages that state it; matches through them are reported as
documented.

A person can state a link no definition or document shows, such as a Python function
that wraps an HTTP API, in a Marshal file's ``links``. Matches through those are
reported as attested, with who attested them.

Nothing is fetched and nothing is matched on a name alone. A URL whose host is a
placeholder, an unresolved ``${...}`` token or a loopback address has no target:
two agents calling ``127.0.0.1`` aren't calling the same system.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Iterator, Optional
from urllib.parse import urlsplit

import yaml

from .model import AgentVersion

_DEFAULT_PORTS = {"http": 80, "https": 443}
# Path parameters as OpenAPI ({id}), templates ({{id}}), Express (:id) and unresolved tokens (${...}) write them.
_PATH_PARAMETER = re.compile(r"\$\{[^}]*\}|\{\{[^}]*\}\}|\{[^}]*\}|(?<=/):[A-Za-z_]\w*")
_PLACEHOLDER = re.compile(r"[{}$<>%]")
_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}

KINDS = {
    "http": "same HTTP operation",
    "mcp": "same MCP server",
    "openapi": "same OpenAPI operation",
    "url": "same knowledge source",
    "lambda": "same Lambda function",
    "ref": "same entity",
    "host": "same API host",
}
"""How a match through each kind of target is described."""


@dataclass(frozen=True)
class Target:
    ref: str
    relation: str
    """``tool``, ``knowledge`` or ``grant``, as in the store."""
    target: str
    basis: str
    """The rule that derived the target, for display beside a match."""
    sources: tuple[str, ...] = ()
    """For a documented equivalent: the vendor pages that state it."""
    attested: Optional[str] = None
    """For a link a person stated: who, when and where."""

    @property
    def qualifier(self) -> str:
        """``documented``, ``attested``, or empty for a target the definition itself states."""
        return "documented" if self.sources else "attested" if self.attested else ""

    @property
    def kind(self) -> str:
        return self.target.split(":", 1)[0]


def strength(target: str) -> int:
    """2 for a target that names one operation, server or source; 1 for a host."""
    return 1 if target.startswith("host:") else 2


def describe(target: str) -> str:
    if target.startswith("lambda:import:"):
        return "same Lambda export"
    if target.startswith("lambda:function:"):
        return "same Lambda function name"  # unique within an account and region, which the template doesn't state
    return KINDS.get(target.split(":", 1)[0], "same target")


def label(target: Target, qualifier: Optional[str] = None) -> str:
    """How a match through ``target`` is described, such as ``same MCP server (documented)``."""
    qualifier = target.qualifier or qualifier or ""
    return describe(target.target) + (f" ({qualifier})" if qualifier else "")


TARGET_KINDS = ("http", "mcp", "openapi", "url", "host", "lambda")


def normalize_target(text: str) -> Optional[str]:
    """A target as a person writes it, in the form resolution compares; None if it isn't one."""
    kind, _, rest = text.strip().partition(":")
    kind = kind.lower()
    if kind not in TARGET_KINDS or not rest:
        return None
    if kind == "http":
        method, _, url = rest.strip().partition(" ")
        normalized = normalize_url(url) if method.upper() in _METHODS else None
        return f"http:{method.upper()} {normalized}" if normalized else None
    if kind == "mcp":
        return mcp_target(rest) if "://" in rest or " " in rest else (f"mcp:{rest}" if rest.startswith(("npm:", "pypi:")) else None)
    if kind == "url":
        normalized = normalize_url(rest)
        return f"url:{normalized}" if normalized else None
    if kind == "host":
        host = rest.strip().lower()
        return f"host:{host}" if host and not _PLACEHOLDER.search(host) and "*" not in host and "/" not in host else None
    return f"{kind}:{rest.strip()}"


def links(record: AgentVersion) -> list[tuple[str, str]]:
    """(ref, target) pairs a Marshal file attests. A link to another ref is the target ``ref:<that ref>``,
    which both refs then share."""
    found = ((record.spec.extensions.get("marshal-file") or {}).get("links")) if record.source.format == "marshal-file" else None
    out: list[tuple[str, str]] = []
    for link in found if isinstance(found, list) else []:
        ref, same_as = link.get("ref"), link.get("same_as")
        if not (isinstance(ref, str) and isinstance(same_as, str)):
            continue
        if "/" in same_as and ":" not in same_as.split("/", 1)[0]:
            out += [(ref, f"ref:{same_as}"), (same_as, f"ref:{same_as}")]
        else:
            out.append((ref, same_as))
    return out


def attestation(record: AgentVersion) -> str:
    """Who attested a Marshal file's links, when and in which file, from the record's source."""
    detail = (record.source.detected_by or "").partition(", ")[2]
    return f"attested in {record.source.artifact}" + (f", {detail}" if detail else "") + f" (agent {record.agent})"


@lru_cache(maxsize=1)
def documented_equivalents() -> dict[str, list[tuple[str, tuple[str, ...]]]]:
    """Ref → [(target, sources)], from every ``equivalents/*.yaml`` file."""
    table: dict[str, list[tuple[str, tuple[str, ...]]]] = {}
    for path in sorted((resources.files("marshal_ledger") / "equivalents").iterdir(), key=lambda p: p.name):
        if not path.name.endswith(".yaml"):
            continue
        for entry in (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("entries") or []:
            table.setdefault(entry["ref"], []).append((entry["target"], tuple(entry["sources"])))
    return table


def normalize_url(url: str) -> Optional[str]:
    """``scheme://host[:port]/path`` with case, default ports, queries, trailing slashes and
    path parameter spellings made uniform; None when the URL doesn't name a reachable host."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:  # a port such as HOST_PORT
        return None
    scheme, host = parts.scheme.lower(), (parts.hostname or "")
    if scheme not in _DEFAULT_PORTS or not host or _PLACEHOLDER.search(parts.netloc):
        return None
    if host == "localhost" or host.endswith(".localhost") or host.startswith("127.") or host in ("::1", "0.0.0.0"):
        return None
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        netloc += f":{port}"
    path = _PATH_PARAMETER.sub("{}", parts.path).rstrip("/")
    return f"{scheme}://{netloc}{path}"


def _host(normalized: str) -> str:
    return "host:" + urlsplit(normalized).netloc


def mcp_target(server: str) -> Optional[str]:
    """The target for an MCP server named by a URL or by a command that runs a published package."""
    if "://" in server:
        url = normalize_url(server)
        return f"mcp:{url}" if url else None
    try:
        words = shlex.split(server)
    except ValueError:
        return None
    if len(words) < 2 or words[0] not in ("npx", "uvx"):
        return None  # a bare name, or a local command, names nothing two agents could share
    package = next((w for w in words[1:] if not w.startswith("-")), None)
    if not package:
        return None
    registry = "npm" if words[0] == "npx" else "pypi"
    # Drop a version: @scope/pkg@1.2.3 → @scope/pkg, pkg==1.2 → pkg.
    package = re.sub(r"(?<=.)@[^/@]+$", "", package) if registry == "npm" else re.split(r"[=<>~!]", package)[0]
    return f"mcp:{registry}:{package.lower()}"


def _mcp_server(ref: str, name: Optional[str]) -> str:
    """The server part of ``tool/mcp.<server>`` or ``tool/mcp.<server>.<tool>``.

    Agent Spec and OASF append the tool's name; since a URL may contain dots, the
    tool's recorded name is what tells the two parts apart.
    """
    server = ref.removeprefix("tool/mcp.")
    if name and server.endswith("." + name) and len(server) > len(name) + 1:
        return server[: -len(name) - 1]
    return server


def _tool_targets(ref: str, name: Optional[str]) -> Iterator[tuple[str, str]]:
    if ref.startswith("tool/http."):
        method, _, url = ref.removeprefix("tool/http.").partition(".")
        normalized = normalize_url(url) if method.upper() in _METHODS else None
        if normalized:
            yield f"http:{method.upper()} {normalized}", "HTTP method and URL"
            yield _host(normalized), "host of the tool's URL"
    elif ref.startswith("tool/mcp."):
        server = _mcp_server(ref, name)
        target = mcp_target(server)
        if target:
            yield target, "MCP server URL" if "://" in server else "package the MCP server command runs"
            if target.startswith("mcp:http"):
                yield _host(target.removeprefix("mcp:")), "host of the MCP server's URL"
    elif ref.startswith("tool/openapi."):
        spec, _, operation = ref.removeprefix("tool/openapi.").rpartition("#")
        if spec and operation:
            if "://" in spec:
                spec = normalize_url(spec) or spec
            yield f"openapi:{spec}#{operation}", "OpenAPI document and operation id"


def _documented(ref: str, relation: str) -> Iterator[Target]:
    for target, sources in documented_equivalents().get(ref, []):
        yield Target(ref, relation, target, "documented equivalent", sources)
        if target.startswith(("mcp:http", "http:", "url:")):
            url = target.split(" ", 1)[-1].removeprefix("mcp:").removeprefix("url:")
            yield Target(ref, relation, _host(url), "host of the documented equivalent", sources)


def targets(record: AgentVersion, attested: Optional[dict[str, list[tuple[str, str]]]] = None) -> list[Target]:
    """Every target the refs of one version reach.

    ``attested`` maps refs to (target, attestation) pairs from Marshal files' links; any
    version that uses a linked ref gets the link's target.
    """
    out: list[Target] = []
    attested = attested or {}
    for use in record.spec.tools:
        out += _documented(use.ref, "tool")
        out += [Target(use.ref, "tool", target, "link stated by a person", attested=who) for target, who in attested.get(use.ref, [])]
    for item in record.spec.knowledge:
        out += _documented(item.ref, "knowledge")
        out += [Target(item.ref, "knowledge", target, "link stated by a person", attested=who)
                for target, who in attested.get(item.ref, [])]
    for use in record.spec.tools:
        out += [Target(use.ref, "tool", t, basis) for t, basis in _tool_targets(use.ref, use.name)]
    for item in record.spec.knowledge:
        url = item.ref.removeprefix("knowledge/")
        normalized = normalize_url(url) if "://" in url else None
        if normalized:
            out.append(Target(item.ref, "knowledge", f"url:{normalized}", "knowledge source URL"))
    executors = record.bindings.get("executors")
    for ref, found in (executors.items() if isinstance(executors, dict) else ()):
        for executor in found if isinstance(found, list) else [found]:
            out.append(Target(ref, "tool", f"lambda:{executor}", "Lambda function that runs the action group"))
    for grant in record.spec.permissions.grants:
        out += [Target(grant.target, "grant", target, "link stated by a person", attested=who)
                for target, who in attested.get(grant.target, [])]
        if grant.target.startswith("other/network."):
            host = grant.target.removeprefix("other/network.").lower()
            if host and not _PLACEHOLDER.search(host) and "*" not in host:
                out.append(Target(grant.target, "grant", f"host:{host}", "network host the agent may reach"))
    return list(dict.fromkeys(out))


__all__ = ["KINDS", "TARGET_KINDS", "Target", "attestation", "describe", "documented_equivalents", "label", "links",
           "mcp_target", "normalize_target", "normalize_url", "strength", "targets"]
