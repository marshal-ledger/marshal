"""Reader for AGNTCY Open Agentic Schema Framework (OASF) records.

An OASF record (https://docs.agntcy.org/oasf/) describes an agent for
discovery: name, version, authors, skills and domains from the OASF taxonomy,
locators, and modules that detail its language models and MCP servers. It
carries no instructions or permissions, so the reader maps metadata, models
and MCP tools, and keeps every module intact under ``extensions.oasf``.

MCP connection environment variables are recorded by name only; their default
values and any connection headers are never copied.

Mapping rules are documented in ``docs/formats/oasf.md``.
"""

from __future__ import annotations

from typing import Any

from ..model import (
    MARSHAL_NAMESPACE,
    AgentSpec,
    Entity,
    EntityKind,
    KnowledgeUse,
    Source,
    ToolUse,
    Value,
)
from ..redact import redact
from .base import DocumentReader, ReadOptions, ReadResult, build_record

FORMAT = "oasf"
ANNOTATION_AGENT = f"{MARSHAL_NAMESPACE}/agent"
ANNOTATION_PLATFORM = f"{MARSHAL_NAMESPACE}/platform"
LANGUAGE_MODEL = "core/language_model"
MCP = "integration/mcp"


def mcp_server(data: dict[str, Any]) -> str:
    """The identity of an MCP server in tool refs: its URL, else its declared name."""
    for connection in data.get("connections") or []:
        if connection.get("url"):
            return str(connection["url"])
    return str(data.get("name") or "unknown")


def _sanitize_module(module: dict[str, Any]) -> dict[str, Any]:
    """A module with MCP credentials removed: env var defaults and connection headers."""
    if module.get("name") != MCP:
        return module
    data = dict(module.get("data") or {})
    connections = []
    for connection in data.get("connections") or []:
        connection = {k: v for k, v in connection.items() if k != "headers"}
        if connection.get("env_vars"):
            connection["env_vars"] = [
                {k: v for k, v in var.items() if k != "default_value"} for var in connection["env_vars"]
            ]
        connections.append(connection)
    if connections:
        data["connections"] = connections
    data.pop("mcp_data", None)
    return {**module, "data": data}


class OasfReader(DocumentReader):
    format = FORMAT
    platform = FORMAT

    @classmethod
    def matches(cls, data: Any) -> bool:
        return (
            isinstance(data, dict)
            and isinstance(data.get("schema_version"), str)
            and isinstance(data.get("name"), str)
            and ("skills" in data or "modules" in data)
        )

    def read_data(self, data: Any, artifact: str, options: ReadOptions | None = None) -> ReadResult:
        options = options or ReadOptions()
        result = ReadResult()
        record: dict[str, Any] = data
        annotations = dict(record.get("annotations") or {})
        agent = annotations.pop(ANNOTATION_AGENT, None) or record["name"]
        platform = annotations.pop(ANNOTATION_PLATFORM, None) or FORMAT
        entities: dict[str, Entity] = {}

        spec = AgentSpec()
        spec.display_name = Value[str].declared(record["name"], "name")
        if record.get("description"):
            text, count = redact(str(record["description"]))
            if count:
                result.warnings.append(f"{artifact}: description: redacted {count} credential-like value(s)")
            spec.description = Value[str].declared(text, "description")

        bindings: dict[str, Any] = {}
        modules = [_sanitize_module(m) for m in record.get("modules") or [] if isinstance(m, dict)]
        for index, module in enumerate(modules):
            data_ = module.get("data") or {}
            if module.get("name") == LANGUAGE_MODEL:
                models = data_.get("models") or []
                if len(models) == 1:
                    model = models[0]
                    if model.get("model"):
                        spec.model.requested = Value[str].declared(str(model["model"]), f"modules[{index}].data.models[0].model")
                    if model.get("provider"):
                        spec.model.provider = Value[str].declared(str(model["provider"]).lower(), f"modules[{index}].data.models[0].provider")
                    if model.get("api_base"):
                        bindings["language_model.api_base"] = model["api_base"]
                elif len(models) > 1:
                    spec.model.requested = Value[str].unknown(f"the record lists {len(models)} models")
            elif module.get("name") == MCP:
                server = mcp_server(data_)
                tools = data_.get("tools") or []
                for tool in tools:
                    ref = f"tool/mcp.{server}.{tool.get('name')}"
                    spec.tools.append(ToolUse(ref=ref, name=tool.get("name"), description=tool.get("description")))
                    entities.setdefault(ref, Entity(ref=ref, kind=EntityKind.TOOL, platform=platform,
                                                    identifier=ref.split("/", 1)[1], display_name=tool.get("title")))
                if not tools:
                    ref = f"tool/mcp.{server}"
                    spec.tools.append(ToolUse(ref=ref, name=data_.get("name"), description=data_.get("description")))
                    entities.setdefault(ref, Entity(ref=ref, kind=EntityKind.TOOL, platform=platform,
                                                    identifier=ref.split("/", 1)[1], display_name=data_.get("name")))
                for resource in data_.get("resources") or []:
                    ref = f"knowledge/mcp.{server}.{resource.get('name')}"
                    spec.knowledge.append(
                        KnowledgeUse(ref=ref, name=resource.get("name"), description=resource.get("description"), type="mcp_resource")
                    )
                    entities.setdefault(ref, Entity(ref=ref, kind=EntityKind.KNOWLEDGE, platform=platform,
                                                    identifier=ref.split("/", 1)[1], display_name=resource.get("name")))

        extension: dict[str, Any] = {}
        for key in ("schema_version", "version", "authors", "created_at", "skills", "domains", "locators"):
            if record.get(key) not in (None, [], {}):
                extension[key] = record[key]
        if annotations:
            extension["annotations"] = annotations
        if modules:
            extension["modules"] = modules
        known = {"name", "description", "annotations", "modules", "schema_version", "version", "authors",
                 "created_at", "skills", "domains", "locators"}
        for key, value in record.items():
            if key not in known:
                extension[key] = value
        spec.extensions[FORMAT] = extension

        result.records.append(
            build_record(
                agent=str(agent),
                platform=str(platform),
                source=Source(
                    format=FORMAT,
                    artifact=artifact,
                    detected_by="OASF record",
                    commit=options.commit,
                    pipeline_run=options.pipeline_run,
                ),
                spec=spec,
                bindings=bindings,
            )
        )
        result.entities = sorted(entities.values(), key=lambda e: e.ref)
        return result

