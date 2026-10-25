"""Writer for AGNTCY OASF 1.1 records."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..model import AgentVersion
from ..readers.oasf import ANNOTATION_AGENT, ANNOTATION_PLATFORM, FORMAT, LANGUAGE_MODEL, MCP
from .base import Writer, Written

SCHEMA_VERSION = "1.1.0"
_MODULE_IDS = {LANGUAGE_MODEL: 103, MCP: 202}


def _connection(server: str) -> dict[str, Any]:
    if server.startswith(("http://", "https://")):
        return {"type": "streamable-http", "url": server}
    return {"type": "stdio", "command": server}


class OasfWriter(Writer):
    format = FORMAT
    suffix = ".oasf.json"
    reader_format = FORMAT
    LOSSES = {
        "instructions": "OASF records describe capabilities for discovery, not instructions",
        "permissions.*": "OASF records do not describe identity or permissions",
        "tools[*].scope": "OASF has no tool scope",
        "tools[*].reversible": "OASF has no reversibility field",
        "tools[*].requires_confirmation": "OASF has no confirmation field",
        "tools[*].input_schema": "OASF lists MCP tools by name and description only",
        "tools[*].output_schema": "OASF lists MCP tools by name and description only",
        "tools[*]": "OASF lists tools only as part of MCP server modules",
        "knowledge[*]": "OASF lists data sources only as MCP server resources",
        "model.parameters.*": "OASF model entries have no generation parameters",
        "model.resolved": "OASF describes the configured model only",
    }
    MECHANICS = ("extensions.oasf.schema_version",)

    def __init__(self, now: datetime | None = None):
        self.now = now

    def write(self, record: AgentVersion) -> Written:
        spec = record.spec
        notes: dict[str, str] = {}
        ext: dict[str, Any] = dict(spec.extensions.get(FORMAT, {}))
        for platform in spec.extensions:
            if platform != FORMAT:
                notes[f"extensions.{platform}.*"] = f"{platform}-specific; no OASF equivalent"

        name = spec.display_name.value or record.agent
        if not spec.display_name.is_known:
            notes["display_name"] = "OASF requires a name; the agent id was used"
        description = spec.description.value
        if not description:
            description = name
            notes["description"] = "OASF requires a description; the name was used"
        version = ext.pop("version", None)
        if version is None:
            version = "0.0.0"
            notes["extensions.oasf.version"] = "OASF requires a version; 0.0.0 was written"
        created_at = ext.pop("created_at", None)
        if created_at is None:
            created_at = (self.now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
            notes["extensions.oasf.created_at"] = "OASF requires a creation time; the conversion time was written"

        doc: dict[str, Any] = {
            "name": name,
            # A record's own content is copied as-is, so it keeps its own schema version.
            "schema_version": ext.pop("schema_version", SCHEMA_VERSION),
            "version": str(version),
            "description": description,
            "authors": ext.pop("authors", []),
            "created_at": created_at,
            "skills": ext.pop("skills", []),
        }
        warnings = []
        if not doc["authors"]:
            warnings.append("OASF requires at least one author; the source declares none")
        if not doc["skills"]:
            warnings.append("OASF requires at least one skill from its taxonomy; the source declares none")
        for key in ("domains", "locators"):
            if key in ext:
                doc[key] = ext.pop(key)

        annotations = dict(ext.pop("annotations", None) or {})
        if record.agent != name:
            annotations[ANNOTATION_AGENT] = record.agent
        if record.platform != FORMAT:
            annotations[ANNOTATION_PLATFORM] = record.platform
        if annotations:
            doc["annotations"] = annotations

        modules = ext.pop("modules", None)
        if modules is None:
            modules = self._modules(record, notes)
            if modules:
                notes["extensions.oasf.modules"] = "built from the model and MCP tools"
        if modules:
            doc["modules"] = modules
        doc.update(ext)
        return Written(document=doc, notes=notes, warnings=warnings)

    def _modules(self, record: AgentVersion, notes: dict[str, str]) -> list[dict[str, Any]]:
        spec = record.spec
        modules: list[dict[str, Any]] = []
        if spec.model.requested.is_known:
            model: dict[str, Any] = {"model": spec.model.requested.value}
            model["provider"] = spec.model.provider.value if spec.model.provider.is_known else "unknown"
            if not spec.model.provider.is_known:
                notes["model.provider"] = "OASF requires a model provider; 'unknown' was written"
            model["api_base"] = record.bindings.get("language_model.api_base", "")
            modules.append({"id": _MODULE_IDS[LANGUAGE_MODEL], "name": LANGUAGE_MODEL, "data": {"models": [model]}})

        servers: dict[str, list[Any]] = {}
        for use in spec.tools:
            if not use.ref.startswith("tool/mcp."):
                continue
            rest = use.ref.removeprefix("tool/mcp.")
            if use.name and rest.endswith(f".{use.name}"):
                servers.setdefault(rest[: -(len(use.name) + 1)], []).append(use)
            else:
                servers.setdefault(rest, [])
        for server, tools in servers.items():
            data: dict[str, Any] = {"name": server, "connections": [_connection(server)]}
            if tools:
                data["tools"] = [
                    {"name": t.name, **({"description": t.description} if t.description else {})} for t in tools
                ]
            modules.append({"id": _MODULE_IDS[MCP], "name": MCP, "data": data})
        return modules
