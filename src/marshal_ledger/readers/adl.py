"""Reader for Agent Definition Language (ADL) documents.

ADL (https://adl-spec.org) describes one agent per YAML or JSON document: its
model, system prompt, tools, resources, permissions and governance metadata.
Tool hints (``read_only``, ``requires_confirmation``) map to scope and
confirmation directly. Governance blocks with no ledger equivalent (data
classification, lifecycle, security, runtime) are kept intact under
``extensions.adl``.

Mapping rules are documented in ``docs/formats/adl.md``.
"""

from __future__ import annotations

from typing import Any, Optional

from ..hashing import sha256_text
from ..model import (
    MARSHAL_NAMESPACE,
    AgentSpec,
    Entity,
    EntityKind,
    Grant,
    IdentityKind,
    Instructions,
    KnowledgeUse,
    Permissions,
    Provenance,
    Scope,
    Source,
    ToolUse,
    Value,
)
from ..redact import redact
from .base import DocumentReader, ReadOptions, ReadResult, build_record, slug

FORMAT = "adl"
SPEC_VERSIONS = ("0.1", "0.2", "0.3")
_FILE_ACCESS = {"read": Scope.READ, "write": Scope.WRITE, "read_write": Scope.WRITE}
_MODEL_PARAMETERS = ("temperature", "max_tokens")
_ADL_RESOURCE_TYPES = ("vector_store", "knowledge_base", "file", "api", "database")
# Top-level keys mapped into the ledger model. ``permissions`` is mapped to grants
# and also kept whole, since ADL boundaries carry more than grants express.
_MAPPED_KEYS = {"$schema", "name", "description", "model", "system_prompt", "tools", "resources", "permissions"}


def _marshal(block: Any) -> dict[str, Any]:
    if isinstance(block, dict):
        value = (block.get("extensions") or {}).get(MARSHAL_NAMESPACE)
        if isinstance(value, dict):
            return value
    return {}


def _without_marshal(extensions: Any) -> Optional[dict[str, Any]]:
    if not isinstance(extensions, dict):
        return None
    rest = {k: v for k, v in extensions.items() if k != MARSHAL_NAMESPACE}
    return rest or None


def tool_ref(agent_slug: str, tool: dict[str, Any]) -> str:
    """The default entity ref for an ADL tool, used when the document carries none."""
    annotations = tool.get("annotations") or {}
    if annotations.get("openapi_ref") and annotations.get("operation_id"):
        return f"tool/openapi.{annotations['openapi_ref']}#{annotations['operation_id']}"
    return f"tool/adl.{agent_slug}.{tool.get('name')}"


def resource_ref(agent_slug: str, resource: dict[str, Any]) -> str:
    if resource.get("uri"):
        return f"knowledge/{resource['uri']}"
    return f"knowledge/adl.{agent_slug}.{resource.get('name')}"


class AdlReader(DocumentReader):
    format = FORMAT
    platform = FORMAT

    @classmethod
    def matches(cls, data: Any) -> bool:
        return isinstance(data, dict) and isinstance(data.get("adl_spec"), str) and "name" in data

    def read_data(self, data: Any, artifact: str, options: ReadOptions | None = None) -> ReadResult:
        options = options or ReadOptions()
        result = ReadResult()
        entities: dict[str, Entity] = {}
        doc: dict[str, Any] = data
        marshal = _marshal(doc)

        if not str(doc["adl_spec"]).startswith(SPEC_VERSIONS):
            result.warnings.append(f"{artifact}: ADL {doc['adl_spec']} is newer than this reader; reading as 0.3")

        name = str(doc.get("name") or "")
        agent = marshal.get("agent") or doc.get("id") or slug(name)
        agent_slug = slug(name)
        platform = marshal.get("platform") or FORMAT

        def clean(text: str, where: str) -> str:
            cleaned, count = redact(text)
            if count:
                result.warnings.append(f"{artifact}: {where}: redacted {count} credential-like value(s)")
            return cleaned

        def entity(kind: EntityKind, ref: str, identifier: str, display: Optional[str] = None) -> None:
            entities.setdefault(ref, Entity(ref=ref, kind=kind, platform=platform, identifier=identifier, display_name=display))

        spec = AgentSpec()
        extension: dict[str, Any] = {}
        if name:
            spec.display_name = Value[str].declared(name, "name")
        if doc.get("description"):
            spec.description = Value[str].declared(clean(str(doc["description"]), "description"), "description")

        # Model
        model = doc.get("model") or {}
        if model.get("name"):
            spec.model.requested = Value[str].declared(str(model["name"]), "model.name")
        if model.get("provider"):
            spec.model.provider = Value[str].declared(str(model["provider"]).lower(), "model.provider")
        spec.model.parameters = {k: model[k] for k in _MODEL_PARAMETERS if k in model}
        model_rest = {k: v for k, v in model.items() if k not in ("name", "provider", *_MODEL_PARAMETERS)}
        if model_rest:
            extension["model"] = model_rest

        # Instructions
        prompt = doc.get("system_prompt")
        if isinstance(prompt, dict):
            if prompt.get("variables") or prompt.get("extensions"):
                extension["system_prompt"] = {k: v for k, v in prompt.items() if k != "template"}
            prompt = prompt.get("template")
        if isinstance(prompt, str) and prompt:
            text = clean(prompt, "system_prompt")
            spec.instructions = Instructions(
                hash=sha256_text(text),
                text=None if options.hash_only else text,
                provenance=Provenance.DECLARED,
                basis="system_prompt",
            )

        # Tools
        tool_extras: dict[str, Any] = {}
        for index, tool in enumerate(doc.get("tools") or []):
            where = f"tools[{index}]"
            ref = _marshal(tool).get("ref") or tool_ref(agent_slug, tool)
            use = ToolUse(
                ref=ref,
                name=tool.get("name"),
                description=clean(str(tool["description"]), where) if tool.get("description") else None,
                input_schema=tool.get("parameters"),
                output_schema=tool.get("returns"),
            )
            if tool.get("read_only") is True:
                use.scope = Value[Scope].declared(Scope.READ, f"{where}.read_only")
                use.reversible = Value[bool].derived(True, "read-only tools have no side effects")
            elif tool.get("read_only") is False:
                use.scope = Value[Scope].derived(Scope.WRITE, f"{where}.read_only is false")
            if isinstance(tool.get("requires_confirmation"), bool):
                use.requires_confirmation = Value[bool].declared(tool["requires_confirmation"], f"{where}.requires_confirmation")
            spec.tools.append(use)
            entity(EntityKind.TOOL, ref, ref.split("/", 1)[1], tool.get("name"))
            extras = {k: v for k, v in tool.items() if k in ("idempotent", "examples", "annotations", "data_classification")}
            if rest := _without_marshal(tool.get("extensions")):
                extras["extensions"] = rest
            if extras:
                tool_extras[str(tool.get("name"))] = extras
        if tool_extras:
            extension["tools"] = tool_extras

        # Resources
        resource_extras: dict[str, Any] = {}
        for resource in doc.get("resources") or []:
            ref = _marshal(resource).get("ref") or resource_ref(agent_slug, resource)
            spec.knowledge.append(
                KnowledgeUse(
                    ref=ref,
                    name=resource.get("name"),
                    description=resource.get("description"),
                    type=resource.get("type"),
                )
            )
            entity(EntityKind.KNOWLEDGE, ref, ref.split("/", 1)[1], resource.get("name"))
            extras = {k: v for k, v in resource.items() if k in ("uri", "mime_types", "schema", "annotations", "data_classification")}
            if rest := _without_marshal(resource.get("extensions")):
                extras["extensions"] = rest
            if extras:
                resource_extras[str(resource.get("name"))] = extras
        if resource_extras:
            extension["resources"] = resource_extras

        # Identity and permissions
        identity = doc.get("cryptographic_identity") or {}
        permissions = Permissions()
        if identity.get("did"):
            permissions.identity = Value[IdentityKind].derived(
                IdentityKind.SERVICE_PRINCIPAL, "cryptographic_identity gives the agent its own identity"
            )
            permissions.principal = Value[str].declared(str(identity["did"]), "cryptographic_identity.did")
        boundaries = doc.get("permissions") or {}
        for path in (boundaries.get("filesystem") or {}).get("allowed_paths") or []:
            scope = _FILE_ACCESS.get(str(path.get("access")))
            if scope:
                target = f"store/file.{path.get('path')}"
                permissions.grants.append(
                    Grant(target=target, privilege=str(path.get("access")), scope=scope, basis="permissions.filesystem")
                )
                entity(EntityKind.STORE, target, f"file.{path.get('path')}")
        for host in (boundaries.get("network") or {}).get("allowed_hosts") or []:
            target = f"other/network.{host}"
            permissions.grants.append(
                Grant(target=target, privilege="connect", scope=Scope.EXECUTE, basis="permissions.network")
            )
            entity(EntityKind.OTHER, target, f"network.{host}")
        spec.permissions = permissions
        if boundaries:
            extension["permissions"] = boundaries

        # Everything else, including fields that ADL profiles add, is kept intact.
        for key, value in doc.items():
            if key in _MAPPED_KEYS:
                continue
            if key == "extensions":
                if rest := _without_marshal(value):
                    extension["extensions"] = rest
                continue
            extension[key] = value
        spec.extensions[FORMAT] = extension

        record = build_record(
            agent=str(agent),
            platform=str(platform),
            source=Source(
                format=FORMAT,
                artifact=artifact,
                detected_by="adl_spec document",
                commit=options.commit,
                pipeline_run=options.pipeline_run,
            ),
            spec=spec,
        )
        result.records.append(record)
        result.entities = sorted(entities.values(), key=lambda e: e.ref)
        return result
