"""Writer for Agent Definition Language (ADL) 0.3 documents."""

from __future__ import annotations

import re
from typing import Any

from ..model import MARSHAL_NAMESPACE, AgentVersion, Scope
from ..readers.adl import FORMAT, resource_ref, tool_ref
from ..readers.base import slug
from .base import Writer, Written

SPEC_VERSION = "0.3.0"
SCHEMA_URL = "https://adl-spec.org/0.3/schema.json"
_RESOURCE_TYPES = {
    "vector_store": "vector_store",
    "knowledge_base": "knowledge_base",
    "file": "file",
    "api": "api",
    "database": "database",
    "table": "database",
    "volume": "file",
}
_MODEL_PARAMETERS = ("temperature", "max_tokens")


def tool_name(text: str) -> str:
    """An ADL-valid tool name (``^[a-z][a-z0-9_]*$``) from a tool name or an entity ref."""
    name = re.sub(r"[^a-z0-9_]+", "_", text.split("/", 1)[-1].lower()).strip("_")
    return name if name[:1].isalpha() else f"tool_{name}"


def _with_marshal(block: dict[str, Any], values: dict[str, Any]) -> None:
    if values:
        block.setdefault("extensions", {})[MARSHAL_NAMESPACE] = values


class AdlWriter(Writer):
    format = FORMAT
    suffix = ".adl.yaml"
    reader_format = FORMAT
    LOSSES = {
        "model.resolved": "ADL describes the requested model only",
        "model.parameters.*": "ADL model configuration has temperature and max_tokens only",
        "tools[*].reversible": "ADL has no reversibility field; only read-only tools read back as reversible",
        "knowledge[*].scope": "ADL resources don't declare an access scope",
        "permissions.access[*]": "ADL has no access list for who may use the agent",
        "permissions.grants[*]": "ADL permissions express network and filesystem boundaries only",
        "permissions.identity": "ADL identifies an agent only by a DID",
        "permissions.principal": "ADL identifies an agent only by a DID",
    }
    MECHANICS = ("extensions.adl.adl_spec",)

    def write(self, record: AgentVersion) -> Written:
        spec = record.spec
        notes: dict[str, str] = {}
        ext: dict[str, Any] = {k: v for k, v in spec.extensions.get(FORMAT, {}).items()}
        for platform in spec.extensions:
            if platform != FORMAT:
                notes[f"extensions.{platform}.*"] = f"{platform}-specific; no ADL equivalent"

        name = spec.display_name.value or record.agent
        if not spec.display_name.is_known:
            notes["display_name"] = "ADL requires a name; the agent id was used"
        description = spec.description.value
        if not description:
            description = name
            notes["description"] = "ADL requires a description; the name was used"
        version = ext.pop("version", None)
        if version is None:
            version = "0.0.0"
            notes["extensions.adl.version"] = "ADL requires a semantic version; 0.0.0 was written"

        # Governance blocks from an ADL source are copied as-is, so they keep their spec version.
        spec_version = str(ext.pop("adl_spec", SPEC_VERSION))
        major_minor = ".".join(spec_version.split(".")[:2])
        doc: dict[str, Any] = {
            "$schema": f"https://adl-spec.org/{major_minor}/schema.json",
            "adl_spec": spec_version,
            "name": name,
            "description": description,
            "version": str(version),
        }
        if "id" in ext:
            doc["id"] = ext.pop("id")
        elif ":" in record.agent:
            doc["id"] = record.agent
            notes["extensions.adl.id"] = "the agent id is a URI, so it was written as the ADL id"
        doc_slug = slug(name)
        marshal: dict[str, Any] = {}
        if record.agent != doc.get("id", doc_slug):
            marshal["agent"] = record.agent
        if record.platform != FORMAT:
            marshal["platform"] = record.platform

        # Model
        model: dict[str, Any] = dict(ext.pop("model", {}) or {})
        if spec.model.requested.is_known:
            model["name"] = spec.model.requested.value
        if spec.model.provider.is_known:
            model["provider"] = spec.model.provider.value
        for key, value in spec.model.parameters.items():
            if key in _MODEL_PARAMETERS:
                model[key] = value
        if model:
            doc["model"] = model

        # Instructions
        if spec.instructions.text:
            prompt_extras = ext.pop("system_prompt", None)
            doc["system_prompt"] = {"template": spec.instructions.text, **prompt_extras} if prompt_extras else spec.instructions.text
        elif spec.instructions.hash:
            notes["instructions"] = "instruction text is not available (hash-only source)"

        # Tools
        tool_extras = ext.pop("tools", {}) or {}
        tools = []
        used_names: set[str] = set()
        for use in spec.tools:
            base = f"tools[{use.ref}]"
            name_ = tool_name(use.name) if use.name else tool_name(use.ref)
            if use.name and name_ != use.name:
                notes[f"{base}.name"] = "ADL tool names must match ^[a-z][a-z0-9_]*$"
            elif not use.name:
                notes[f"{base}.name"] = "ADL requires a tool name; one was derived from the ref"
            while name_ in used_names:
                name_ += "_"
            used_names.add(name_)
            tool: dict[str, Any] = {"name": name_}
            if use.description:
                tool["description"] = use.description
            else:
                tool["description"] = f"Tool {use.ref.split('/', 1)[-1]}"
                notes[f"{base}.description"] = "ADL requires a tool description; one was generated from the ref"
            if use.input_schema is not None:
                tool["parameters"] = use.input_schema
            if use.output_schema is not None:
                tool["returns"] = use.output_schema
            scope = use.scope.value if use.scope.is_known else None
            if scope is Scope.READ:
                tool["read_only"] = True
            elif scope is not None:
                tool["read_only"] = False
                if scope is not Scope.WRITE:
                    notes[f"{base}.scope"] = f"ADL can only say whether a tool is read-only; {scope.value} reads back as write"
            if use.requires_confirmation.is_known:
                tool["requires_confirmation"] = use.requires_confirmation.value
            for key, value in (tool_extras.get(use.name or "") or {}).items():
                if key == "extensions":
                    tool.setdefault("extensions", {}).update(value)
                else:
                    tool.setdefault(key, value)
            if use.ref != tool_ref(doc_slug, tool):
                _with_marshal(tool, {"ref": use.ref})
            tools.append(tool)
        if tools:
            doc["tools"] = tools

        # Resources
        resource_extras = ext.pop("resources", {}) or {}
        resources = []
        for item in spec.knowledge:
            base = f"knowledge[{item.ref}]"
            extras = resource_extras.get(item.name or "") or {}
            resource: dict[str, Any] = {"name": item.name or item.ref.split("/", 1)[-1]}
            if not item.name:
                notes[f"{base}.name"] = "ADL requires a resource name; the ref was used"
            kind = _RESOURCE_TYPES.get(item.type or "")
            if kind is None:
                kind = "knowledge_base"
                notes[f"{base}.type"] = f"ADL resource types are fixed; {item.type or 'an unknown type'} was written as knowledge_base"
            elif kind != item.type:
                notes[f"{base}.type"] = f"ADL has no {item.type} resource type"
            resource["type"] = kind
            if item.description:
                resource["description"] = item.description
            if not extras.get("uri") and item.ref.startswith("knowledge/") and "://" in item.ref:
                resource["uri"] = item.ref.split("/", 1)[1]
                notes["extensions.adl.resources"] = "resource URIs were written from the knowledge refs"
            for key, value in extras.items():
                if key == "extensions":
                    resource.setdefault("extensions", {}).update(value)
                else:
                    resource.setdefault(key, value)
            if item.ref != resource_ref(doc_slug, resource):
                _with_marshal(resource, {"ref": item.ref})
            resources.append(resource)
        if resources:
            doc["resources"] = resources

        # Permissions: ADL boundaries round-trip whole; grants from other formats map where they can.
        boundaries = ext.pop("permissions", None)
        if boundaries is None:
            boundaries = {}
            for grant in spec.permissions.grants:
                if grant.target.startswith("store/file."):
                    access = {Scope.READ: "read"}.get(grant.scope, "read_write")
                    boundaries.setdefault("filesystem", {}).setdefault("allowed_paths", []).append(
                        {"path": grant.target.removeprefix("store/file."), "access": access}
                    )
                elif grant.target.startswith("other/network."):
                    boundaries.setdefault("network", {}).setdefault("allowed_hosts", []).append(
                        grant.target.removeprefix("other/network.")
                    )
        if boundaries:
            doc["permissions"] = boundaries

        principal = spec.permissions.principal.value if spec.permissions.principal.is_known else None
        if "cryptographic_identity" not in ext and principal and str(principal).startswith("did:"):
            ext["cryptographic_identity"] = {"did": principal}

        if "data_classification" not in ext:
            ext["data_classification"] = {"sensitivity": "restricted"}
            notes["extensions.adl.data_classification"] = (
                "ADL requires a data classification; the most restrictive level was written for review"
            )

        vendor_extensions = ext.pop("extensions", None) or {}
        doc.update(ext)
        if vendor_extensions or marshal:
            doc["extensions"] = dict(vendor_extensions)
            _with_marshal(doc, marshal)
        return Written(document=doc, notes=notes)
