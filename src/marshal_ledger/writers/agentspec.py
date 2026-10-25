"""Writer for Open Agent Specification (Agent Spec) 26.3 documents."""

from __future__ import annotations

import uuid
from typing import Any

from ..model import MARSHAL_NAMESPACE, AgentVersion
from ..readers.agentspec import _PROVIDERS, FORMAT, schema_to_properties
from ..readers.base import slug
from .adl import tool_name
from .base import Writer, Written

SPEC_VERSION = "26.3.1"
_NEEDS_URL = {"OllamaConfig", "VllmConfig"}
_ENUM_PROVIDER = {"VllmConfig", "OllamaConfig", "OciGenAiConfig"}
_MODEL_PROVIDERS = {"COHERE", "GROK", "META", "OTHER", "XAI"}


def _transport_for(server: str) -> dict[str, Any]:
    if server.startswith(("http://", "https://")):
        return {"component_type": "StreamableHTTPTransport", "name": "transport", "url": server}
    return {"component_type": "StdioTransport", "name": "transport", "command": server}


class AgentSpecWriter(Writer):
    format = FORMAT
    suffix = ".agentspec.yaml"
    reader_format = FORMAT
    LOSSES = {
        "knowledge[*]": "Agent Spec has no knowledge-source component; retrieval is modeled as a tool",
        "permissions.*": "Agent Spec does not describe identity or permissions",
        "tools[*].scope": "Agent Spec states a tool's effect only through a remote tool's HTTP method",
        "tools[*].reversible": "Agent Spec has no reversibility field",
        "model.resolved": "Agent Spec describes the configured model only",
    }

    def write(self, record: AgentVersion) -> Written:
        spec = record.spec
        notes: dict[str, str] = {}
        ext: dict[str, Any] = dict(spec.extensions.get(FORMAT, {}))
        for platform in spec.extensions:
            if platform != FORMAT:
                notes[f"extensions.{platform}.*"] = f"{platform}-specific; no Agent Spec equivalent"

        name = spec.display_name.value or record.agent
        if not spec.display_name.is_known:
            notes["display_name"] = "Agent Spec requires a name; the agent id was used"
        agent_slug = slug(name)

        metadata: dict[str, Any] = dict(ext.pop("metadata", None) or {})
        marshal: dict[str, Any] = {"agent": record.agent}
        if record.platform != FORMAT:
            marshal["platform"] = record.platform
        metadata[MARSHAL_NAMESPACE] = marshal

        component: dict[str, Any] = {
            "component_type": "Agent",
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"marshal:{record.agent}")),
            "name": name,
        }
        if spec.description.is_known:
            component["description"] = spec.description.value
        component["metadata"] = metadata
        for key in ("inputs", "outputs"):
            if key in ext:
                component[key] = ext.pop(key)
        component["llm_config"] = self._llm_config(record, ext.pop("llm_config", None), notes)
        if spec.instructions.text:
            component["system_prompt"] = spec.instructions.text
        else:
            component["system_prompt"] = ""
            if spec.instructions.hash:
                notes["instructions"] = "instruction text is not available (hash-only source)"

        toolboxes = ext.pop("toolboxes", {}) or {}
        boxed = {ref for box in toolboxes.values() for ref in box.get("refs", [])}
        tool_extras = ext.pop("tools", {}) or {}
        tools = []
        for use in spec.tools:
            if use.ref in boxed:
                continue
            tools.append(self._tool(use, tool_extras.get(use.name or "") or {}, agent_slug, notes))
        component["tools"] = tools
        if toolboxes:
            component["toolboxes"] = [self._toolbox(name_, box) for name_, box in toolboxes.items()]
        for key in ("human_in_the_loop", "transforms"):
            if key in ext:
                component[key] = ext.pop(key)
        component["agentspec_version"] = SPEC_VERSION
        return Written(document=component, notes=notes)

    def _llm_config(self, record: AgentVersion, base: dict[str, Any] | None, notes: dict[str, str]) -> dict[str, Any]:
        model = record.spec.model
        provider = model.provider.value if model.provider.is_known else None
        config: dict[str, Any] = dict(base or {})
        if not base:
            notes["extensions.agent-spec.llm_config"] = "Agent Spec requires an LLM config; one was built from the model fields"
        ctype = config.get("component_type") or ("OpenAiConfig" if provider == "openai" else "LlmConfig")
        url = record.bindings.get("llm_config.url")
        if ctype in _NEEDS_URL and not url:
            notes["extensions.agent-spec.llm_config"] = (
                f"the {ctype} endpoint URL is environment-bound and not in the record; written as a generic LlmConfig"
            )
            ctype = "LlmConfig"
            config = {k: v for k, v in config.items() if k in ("name", "api_type")}
        config["component_type"] = ctype
        config.setdefault("name", f"{slug(str(model.requested.value or 'model'))}-config")
        if model.requested.is_known:
            config["model_id"] = model.requested.value
        else:
            config["model_id"] = "unknown"
            notes["model.requested"] = "Agent Spec requires a model id; 'unknown' was written"
        if provider and provider != _PROVIDERS.get(ctype):
            if ctype in _ENUM_PROVIDER:
                if provider.upper() in _MODEL_PROVIDERS:
                    config["provider"] = provider.upper()
                else:
                    notes["model.provider"] = f"{ctype} accepts only {', '.join(sorted(_MODEL_PROVIDERS))} as a provider"
            else:
                config["provider"] = provider
        if url:
            config["url"] = url
        if model.parameters:
            config["default_generation_parameters"] = dict(model.parameters)
        return config

    def _tool(self, use: Any, extra: dict[str, Any], agent_slug: str, notes: dict[str, str]) -> dict[str, Any]:
        name = use.name or tool_name(use.ref)
        if not use.name:
            notes[f"tools[{use.ref}].name"] = "Agent Spec requires a tool name; one was derived from the ref"
        identifier = use.ref.split("/", 1)[-1]
        ctype = extra.get("component_type")
        if ctype is None:
            if identifier.startswith("http."):
                ctype = "RemoteTool"
            elif identifier.startswith("mcp."):
                ctype = "MCPTool"
            elif identifier.startswith("agentspec.builtin."):
                ctype = "BuiltinTool"
            else:
                ctype = "ServerTool"
        tool: dict[str, Any] = {"component_type": ctype, "name": name}
        if use.description:
            tool["description"] = use.description
        if inputs := schema_to_properties(use.input_schema):
            tool["inputs"] = inputs
            required = set((use.input_schema or {}).get("required") or [])
            if any(p["title"] not in required and "default" not in p for p in inputs):
                notes[f"tools[{use.ref}].input_schema"] = (
                    "Agent Spec marks a parameter optional only by a default; optional parameters without one read back as required"
                )
        if use.output_schema is not None and not (use.output_schema.get("properties")):
            # Agent Spec names every output; a bare return schema becomes one output called "result".
            tool["outputs"] = [{"title": "result", **use.output_schema}]
            notes[f"tools[{use.ref}].output_schema"] = "Agent Spec names every output; the return value was written as 'result'"
        elif outputs := schema_to_properties(use.output_schema):
            tool["outputs"] = outputs
        if use.requires_confirmation.is_known:
            tool["requires_confirmation"] = bool(use.requires_confirmation.value)
        else:
            notes[f"tools[{use.ref}].requires_confirmation"] = "Agent Spec defaults requires_confirmation to false"

        default_ref = None
        if ctype == "RemoteTool":
            method, _, url = identifier.removeprefix("http.").partition(".")
            tool["http_method"] = extra.get("http_method") or method
            tool["url"] = extra.get("url") or url
            for key in ("api_spec_uri", "data", "query_params", "url_allow_list"):
                if key in extra:
                    tool[key] = extra[key]
            default_ref = f"tool/http.{str(tool['http_method']).upper()}.{tool['url']}"
        elif ctype == "MCPTool":
            transport = extra.get("client_transport")
            if not transport:
                server = identifier.removeprefix("mcp.")
                server = server[: -(len(name) + 1)] if server.endswith(f".{name}") else server
                transport = _transport_for(server)
                notes["extensions.agent-spec.tools"] = "MCP tool transports were built from the tool refs"
            tool["client_transport"] = transport
            server = transport.get("url") or transport.get("command") or transport.get("name")
            default_ref = f"tool/mcp.{server}.{name}"
        elif ctype == "BuiltinTool":
            tool["tool_type"] = extra.get("tool_type") or identifier.removeprefix("agentspec.builtin.")
            for key in ("configuration", "executor_name", "tool_version"):
                if key in extra:
                    tool[key] = extra[key]
            default_ref = f"tool/agentspec.builtin.{tool['tool_type']}"
        else:
            default_ref = f"tool/agentspec.{agent_slug}.{name}"
        metadata = dict(extra.get("metadata") or {})
        if use.ref != default_ref:
            metadata[MARSHAL_NAMESPACE] = {"ref": use.ref}
        if metadata:
            tool["metadata"] = metadata
        return tool

    def _toolbox(self, name: str, box: dict[str, Any]) -> dict[str, Any]:
        written = {k: v for k, v in box.items() if k != "refs"}
        written.setdefault("component_type", "MCPToolBox")
        written.setdefault("name", name)
        return written
