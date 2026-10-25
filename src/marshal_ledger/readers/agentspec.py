"""Reader for Open Agent Specification (Agent Spec) documents.

Agent Spec (https://github.com/oracle/agent-spec) describes agents and flows as
typed components that may reference each other by id. The reader resolves
references, then records every ``Agent`` component it finds, including agents
nested in flows, swarms and manager-worker groups. The orchestration components
themselves are reported as skipped.

Credentials in LLM configs and tool transports (API keys, sensitive headers,
TLS key files, auth configs, environment) are never copied. Endpoint URLs are
environment-bound and go to ``bindings``.

Mapping rules are documented in ``docs/formats/agent-spec.md``.
"""

from __future__ import annotations

from typing import Any, Iterator, Optional

from ..hashing import sha256_text
from ..model import (
    MARSHAL_NAMESPACE,
    AgentSpec,
    Entity,
    EntityKind,
    Instructions,
    Provenance,
    Scope,
    Source,
    ToolUse,
    Value,
)
from ..redact import redact
from .base import DocumentReader, ReadOptions, ReadResult, Skipped, build_record, slug

FORMAT = "agent-spec"
_PROVIDERS = {
    "OpenAiConfig": "openai",
    "OciGenAiConfig": "oci",
    "OllamaConfig": "ollama",
    "VllmConfig": "vllm",
    "GeminiConfig": "google",
}
_ORCHESTRATION = {"Flow", "Swarm", "ManagerWorkers"}
_REMOTE_AGENTS = {"A2AAgent", "OciAgent", "RemoteAgent"}
_TOOL_TYPES = {"ServerTool", "ClientTool", "RemoteTool", "MCPTool", "BuiltinTool"}
# Never copied from any component.
_SECRET_KEYS = {"api_key", "sensitive_headers", "headers", "key_file", "cert_file", "ca_file", "client_config", "auth", "env"}
# Environment-bound; recorded as bindings.
_ENDPOINT_KEYS = {"url", "compartment_id"}
_READ_METHODS = {"GET", "HEAD", "OPTIONS"}


def _marshal(component: dict[str, Any]) -> dict[str, Any]:
    value = (component.get("metadata") or {}).get(MARSHAL_NAMESPACE)
    return value if isinstance(value, dict) else {}


def _metadata(component: dict[str, Any]) -> Optional[dict[str, Any]]:
    rest = {k: v for k, v in (component.get("metadata") or {}).items() if k != MARSHAL_NAMESPACE}
    return rest or None


def properties_to_schema(properties: Any) -> Optional[dict[str, Any]]:
    """Agent Spec inputs and outputs (a list of titled JSON Schemas) as one object schema."""
    if not properties:
        return None
    props: dict[str, Any] = {}
    required: list[str] = []
    for prop in properties:
        if not isinstance(prop, dict) or "title" not in prop:
            continue
        props[str(prop["title"])] = {k: v for k, v in prop.items() if k != "title"}
        if "default" not in prop:
            required.append(str(prop["title"]))
    schema: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


def schema_to_properties(schema: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    if not schema:
        return []
    return [{"title": name, **sub} for name, sub in (schema.get("properties") or {}).items()]


def _sanitize(component: dict[str, Any]) -> dict[str, Any]:
    """A component with credentials and endpoint values removed, for extensions."""
    clean = {}
    for key, value in component.items():
        if key in _SECRET_KEYS or key in _ENDPOINT_KEYS or key in ("id", "metadata", "$referenced_components"):
            continue
        clean[key] = _sanitize(value) if isinstance(value, dict) else value
    return clean


def transport_server(transport: dict[str, Any]) -> str:
    return str(transport.get("url") or transport.get("command") or transport.get("name") or "unknown")


class _References:
    """Resolves ``{"$component_ref": id}`` against every ``$referenced_components`` block."""

    def __init__(self, document: Any):
        self.components: dict[str, Any] = {}
        self._collect(document)

    def _collect(self, node: Any) -> None:
        if isinstance(node, dict):
            for key, component in (node.get("$referenced_components") or {}).items():
                ident = component.get("id", key) if isinstance(component, dict) else key
                self.components[str(ident)] = component
                self.components.setdefault(str(key), component)
            for value in node.values():
                self._collect(value)
        elif isinstance(node, list):
            for value in node:
                self._collect(value)

    def resolve(self, node: Any, seen: frozenset[str] = frozenset()) -> Any:
        if isinstance(node, dict):
            ref = node.get("$component_ref")
            if isinstance(ref, str) and len(node) == 1:
                if ref in seen or ref not in self.components:
                    return node
                return self.resolve(self.components[ref], seen | {ref})
            return {k: self.resolve(v, seen) for k, v in node.items() if k != "$referenced_components"}
        if isinstance(node, list):
            return [self.resolve(v, seen) for v in node]
        return node


def _walk(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        if "component_type" in node:
            yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


class AgentSpecReader(DocumentReader):
    format = FORMAT
    platform = FORMAT

    @classmethod
    def matches(cls, data: Any) -> bool:
        return (
            isinstance(data, dict)
            and isinstance(data.get("component_type"), str)
            and ("agentspec_version" in data or data["component_type"] in _ORCHESTRATION | {"Agent"})
        )

    def read_data(self, data: Any, artifact: str, options: ReadOptions | None = None) -> ReadResult:
        options = options or ReadOptions()
        result = ReadResult()
        document = _References(data).resolve(data)
        stem = artifact.rsplit("/", 1)[-1].split(".", 1)[0]

        seen_ids: set[str] = set()
        agents: list[dict[str, Any]] = []
        for component in _walk(document):
            ctype = component.get("component_type")
            ident = str(component.get("id") or id(component))
            if ident in seen_ids:
                continue
            seen_ids.add(ident)
            if ctype == "Agent":
                agents.append(component)
            elif ctype in _ORCHESTRATION:
                result.skipped.append(
                    Skipped(artifact, f"{ctype} {component.get('name')}",
                            "orchestration is not represented yet; its member agents are recorded")
                )
            elif ctype in _REMOTE_AGENTS:
                result.skipped.append(
                    Skipped(artifact, f"{ctype} {component.get('name')}", "a reference to an agent defined elsewhere")
                )

        slugs = [slug(str(a.get("name") or "agent")) for a in agents]
        entities: dict[str, Entity] = {}
        for agent, agent_slug in zip(agents, slugs):
            if slugs.count(agent_slug) > 1:
                agent_slug = f"{agent_slug}-{str(agent.get('id', ''))[:8]}"
            record, found = self._agent(agent, agent_slug, stem, artifact, options, result)
            result.records.append(record)
            for entity in found:
                entities.setdefault(entity.ref, entity)
        result.entities = sorted(entities.values(), key=lambda e: e.ref)
        return result

    def _agent(
        self,
        agent: dict[str, Any],
        agent_slug: str,
        stem: str,
        artifact: str,
        options: ReadOptions,
        result: ReadResult,
    ) -> tuple[Any, list[Entity]]:
        marshal = _marshal(agent)
        agent_id = marshal.get("agent") or f"{stem}/{agent_slug}"
        platform = marshal.get("platform") or FORMAT
        where = f"Agent {agent.get('name')}"
        entities: list[Entity] = []
        spec = AgentSpec()
        extension: dict[str, Any] = {}
        bindings: dict[str, Any] = {}

        def clean(text: str, field: str) -> str:
            cleaned, count = redact(text)
            if count:
                result.warnings.append(f"{artifact}: {where}: {field}: redacted {count} credential-like value(s)")
            return cleaned

        if agent.get("name"):
            spec.display_name = Value[str].declared(str(agent["name"]), "name")
        if agent.get("description"):
            spec.description = Value[str].declared(clean(str(agent["description"]), "description"), "description")

        llm = agent.get("llm_config") or {}
        if isinstance(llm, dict) and llm:
            if llm.get("model_id"):
                spec.model.requested = Value[str].declared(str(llm["model_id"]), "llm_config.model_id")
            provider = llm.get("provider") or llm.get("api_provider")
            if provider:
                spec.model.provider = Value[str].declared(str(provider).lower(), "llm_config.provider")
            elif llm.get("component_type") in _PROVIDERS:
                spec.model.provider = Value[str].derived(
                    _PROVIDERS[llm["component_type"]], f"llm_config is a {llm['component_type']}"
                )
            spec.model.parameters = {
                k: v for k, v in (llm.get("default_generation_parameters") or {}).items() if v is not None
            }
            if any(llm.get(k) for k in _SECRET_KEYS):
                result.warnings.append(f"{artifact}: {where}: llm_config credentials were not recorded")
            for key in _ENDPOINT_KEYS:
                if llm.get(key):
                    bindings[f"llm_config.{key}"] = llm[key]
            extension["llm_config"] = {
                k: v
                for k, v in _sanitize(llm).items()
                if k not in ("model_id", "provider", "api_provider", "default_generation_parameters", "description")
            }

        prompt = agent.get("system_prompt")
        if isinstance(prompt, str) and prompt:
            text = clean(prompt, "system_prompt")
            spec.instructions = Instructions(
                hash=sha256_text(text),
                text=None if options.hash_only else text,
                provenance=Provenance.DECLARED,
                basis="system_prompt",
            )

        tool_extras: dict[str, Any] = {}
        for tool in agent.get("tools") or []:
            if not isinstance(tool, dict) or tool.get("component_type") not in _TOOL_TYPES:
                result.warnings.append(f"{artifact}: {where}: unrecognized tool {tool!r:.80}")
                continue
            use, extra = self._tool(tool, agent_slug, artifact, where, result)
            spec.tools.append(use)
            entities.append(Entity(ref=use.ref, kind=EntityKind.TOOL, platform=platform,
                                   identifier=use.ref.split("/", 1)[1], display_name=use.name))
            if extra:
                tool_extras[str(tool.get("name"))] = extra
        if tool_extras:
            extension["tools"] = tool_extras

        toolboxes: dict[str, Any] = {}
        for box in agent.get("toolboxes") or []:
            if not isinstance(box, dict):
                continue
            transport = box.get("client_transport") or {}
            server = transport_server(transport)
            names = [f if isinstance(f, str) else f.get("name") for f in box.get("tool_filter") or []]
            refs = []
            for name in names or [None]:
                ref = f"tool/mcp.{server}.{name}" if name else f"tool/mcp.{server}"
                use = ToolUse(ref=ref, name=name or box.get("name"), description=box.get("description"))
                use.requires_confirmation = Value[bool].declared(bool(box.get("requires_confirmation", False)), "toolbox.requires_confirmation") \
                    if "requires_confirmation" in box else Value[bool].derived(False, "Agent Spec default: requires_confirmation is false")
                spec.tools.append(use)
                entities.append(Entity(ref=ref, kind=EntityKind.TOOL, platform=platform, identifier=ref.split("/", 1)[1], display_name=use.name))
                refs.append(ref)
            sanitized = _sanitize(box)
            if transport.get("url"):
                # The server URL identifies the tools (it is part of their refs), so it is kept.
                sanitized.setdefault("client_transport", {})["url"] = transport["url"]
            toolboxes[str(box.get("name"))] = {"refs": refs, **sanitized}
        if toolboxes:
            extension["toolboxes"] = toolboxes

        for key in ("inputs", "outputs", "human_in_the_loop", "transforms"):
            if agent.get(key) not in (None, [], {}):
                extension[key] = agent[key]
        if metadata := _metadata(agent):
            extension["metadata"] = metadata
        spec.extensions[FORMAT] = extension

        record = build_record(
            agent=str(agent_id),
            platform=str(platform),
            source=Source(
                format=FORMAT,
                artifact=artifact,
                locator=f"Agent {agent.get('id') or agent.get('name')}",
                detected_by="component_type Agent",
                commit=options.commit,
                pipeline_run=options.pipeline_run,
            ),
            spec=spec,
            bindings=bindings,
        )
        return record, entities

    def _tool(
        self, tool: dict[str, Any], agent_slug: str, artifact: str, where: str, result: ReadResult
    ) -> tuple[ToolUse, dict[str, Any]]:
        ctype = tool["component_type"]
        name = str(tool.get("name"))
        extra: dict[str, Any] = {}
        use = ToolUse(
            ref="",
            name=name,
            description=tool.get("description"),
            input_schema=properties_to_schema(tool.get("inputs")),
            output_schema=properties_to_schema(tool.get("outputs")),
        )
        if "requires_confirmation" in tool:
            use.requires_confirmation = Value[bool].declared(bool(tool["requires_confirmation"]), "requires_confirmation")
        else:
            use.requires_confirmation = Value[bool].derived(False, "Agent Spec default: requires_confirmation is false")

        if ctype == "RemoteTool":
            method = str(tool.get("http_method", "")).upper()
            use.ref = f"tool/http.{method}.{tool.get('url')}"
            if method in _READ_METHODS:
                use.scope = Value[Scope].derived(Scope.READ, f"http_method {method}")
                use.reversible = Value[bool].derived(True, "read-only HTTP methods have no side effects")
            elif method:
                use.scope = Value[Scope].derived(Scope.WRITE, f"http_method {method}")
            extra = {k: v for k, v in tool.items() if k in ("component_type", "http_method", "url", "api_spec_uri", "data", "query_params", "url_allow_list")}
            if tool.get("headers"):
                extra["header_names"] = sorted(tool["headers"])
            if tool.get("sensitive_headers"):
                result.warnings.append(f"{artifact}: {where}: tool {name}: sensitive headers were not recorded")
        elif ctype == "MCPTool":
            transport = tool.get("client_transport") or {}
            use.ref = f"tool/mcp.{transport_server(transport)}.{name}"
            extra = {"component_type": ctype, "client_transport": _sanitize(transport)}
            if transport.get("url"):
                extra["client_transport"]["url"] = transport["url"]
        elif ctype == "BuiltinTool":
            use.ref = f"tool/agentspec.builtin.{tool.get('tool_type') or name}"
            extra = {k: v for k, v in tool.items() if k in ("component_type", "tool_type", "configuration", "executor_name", "tool_version")}
        else:
            use.ref = f"tool/agentspec.{agent_slug}.{name}"
            if ctype != "ServerTool":
                extra = {"component_type": ctype}
        if marshal_ref := _marshal(tool).get("ref"):
            use.ref = marshal_ref
        if metadata := _metadata(tool):
            extra["metadata"] = metadata
        return use, extra
