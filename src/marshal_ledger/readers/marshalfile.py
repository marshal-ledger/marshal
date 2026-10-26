"""Reader for Marshal files: hand-written agent definitions.

A Marshal file (``*.marshal.yaml``, marked ``marshal: agent/v0``) records an
agent that no reader covers, such as one on an in-house platform or in a vendor
UI with no export. Its fields map one-to-one to the ledger model.

The values come from a person, not from the platform, so they are recorded as
``attested`` rather than ``declared``, with a basis naming the file and, when
given, who attested them. A field the file leaves out, or sets to ``unknown``,
is unknown.

Free text is redacted. In ``bindings``, ``extensions``, schemas and model
parameters, values under credential-like keys are replaced and environment
variables are kept by name only.

Mapping rules are documented in ``docs/formats/marshal-file.md``.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional

from ..hashing import sha256_text
from ..model import (
    AccessRule,
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
from .base import DocumentReader, ReadOptions, ReadResult, Skipped, build_record

FORMAT = "marshal-file"
KIND = "agent/v0"
UNKNOWN = "unknown"
REDACTED = "<redacted>"
PRINCIPAL_TYPES = ("user", "group", "service_principal", "unknown")
_SECRET_KEY = re.compile(r"(password|passwd|secret|api[_-]?key|access[_-]?key|private[_-]?key|token)$", re.IGNORECASE)
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
TOP_LEVEL_KEYS = {
    "marshal", "id", "platform", "attested_by", "attested_at", "display_name", "description", "model",
    "instructions", "tools", "knowledge", "permissions", "bindings", "extensions",
}
_KINDS = {k.value for k in EntityKind}


def qualify(ref: str, kind: str, platform: str) -> str:
    """An entity ref from a value that may leave out the kind: ``refund`` → ``tool/<platform>.refund``."""
    return ref if "/" in ref else f"{kind}/{platform}.{ref}"


def _ignore(message: str) -> None:
    pass


def clean(value: Any, where: str = "", warn: Callable[[str], None] = _ignore) -> Optional[str]:
    """Free text with credential-like values redacted."""
    if value is None:
        return None
    cleaned, count = redact(str(value))
    if count:
        warn(f"{where}: redacted {count} credential-like value(s)")
    return cleaned


def scrub_block(node: Any, where: str = "", warn: Callable[[str], None] = _ignore) -> Any:
    """A credential-safe copy of a free-form block: values under credential-like keys
    are replaced, environment variables are kept by name, and strings are redacted."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            key = str(key)
            if key == "env" and isinstance(value, dict):
                out[key] = sorted(str(k) for k in value)
                if any(v not in (None, "") for v in value.values()):
                    warn(f"{where}.env: environment variables are recorded by name only")
            elif _SECRET_KEY.search(key) and value not in (None, "", REDACTED):
                out[key] = REDACTED
                warn(f"{where}.{key}: credential-like key; value not copied")
            else:
                out[key] = scrub_block(value, f"{where}.{key}", warn)
        return out
    if isinstance(node, list):
        return [scrub_block(v, f"{where}[{i}]", warn) for i, v in enumerate(node)]
    if isinstance(node, str):
        return clean(node, where, warn)
    return node


class MarshalFileReader(DocumentReader):
    format = FORMAT
    platform = FORMAT

    @classmethod
    def matches(cls, data: Any) -> bool:
        return isinstance(data, dict) and str(data.get("marshal", "")).startswith("agent/")

    def read_data(self, data: Any, artifact: str, options: ReadOptions | None = None) -> ReadResult:
        options = options or ReadOptions()
        result = ReadResult()
        doc: dict[str, Any] = data

        def warn(message: str) -> None:
            result.warnings.append(f"{artifact}: {message}")

        if doc["marshal"] != KIND:
            warn(f"marshal: {doc['marshal']} is not {KIND}; reading as {KIND}")
        missing = [key for key in ("id", "platform") if not doc.get(key)]
        if missing:
            result.skipped.append(Skipped(artifact, "", f"a Marshal file needs {' and '.join(missing)}"))
            return result
        agent, platform = str(doc["id"]), str(doc["platform"])

        attested_by, attested_at = (
            clean(doc[key]) if doc.get(key) not in (None, "", UNKNOWN) else None for key in ("attested_by", "attested_at")
        )
        attestation = " ".join(filter(None, (
            f"attested by {attested_by}" if attested_by else None,
            f"on {attested_at}" if attested_at else None,
        )))

        def basis(path: str) -> str:
            return f"{artifact}#{path}" + (f" ({attestation})" if attestation else "")

        def absent() -> str:
            return f"not stated in the hand-written definition {artifact}"

        def text(value: Any, where: str) -> Optional[str]:
            return clean(value, where, warn)

        def scrub(node: Any, where: str) -> Any:
            return scrub_block(node, where, warn)

        def mapping(key: str) -> dict[str, Any]:
            raw = doc.get(key)
            if raw is None or isinstance(raw, dict):
                return raw or {}
            warn(f"{key}: expected a mapping; ignored")
            return {}

        def items(raw: Any, where: str) -> list[Any]:
            if raw is None or isinstance(raw, list):
                return raw or []
            warn(f"{where}: expected a list; ignored")
            return []

        def value(raw: Any, path: str, kind: Any = str) -> Value:
            """An attested value, or unknown when left out, stated as unknown, or invalid."""
            typed = Value[kind]
            if raw is None:
                return typed.unknown(absent())
            if raw == UNKNOWN:
                return typed.unknown(f"stated as unknown in {basis(path)}")
            try:
                if kind is bool:
                    if not isinstance(raw, bool):
                        raise ValueError
                    parsed: Any = raw
                elif kind is str:
                    parsed = text(raw, path)
                else:
                    parsed = kind(raw)
            except ValueError:
                warn(f"{path}: {raw!r} is not a valid value; recorded as unknown")
                return typed.unknown(f"invalid value in {basis(path)}")
            return typed.attested(parsed, basis(path))

        entities: dict[str, Entity] = {}

        def entity(kind: EntityKind, ref: str, display: Optional[str] = None) -> None:
            head = ref.split("/", 1)[0]
            entities.setdefault(ref, Entity(
                ref=ref, kind=EntityKind(head) if head in _KINDS else kind, platform=platform,
                identifier=ref.split("/", 1)[1], display_name=display,
            ))

        spec = AgentSpec(
            display_name=value(doc.get("display_name"), "display_name"),
            description=value(doc.get("description"), "description"),
        )

        model = {"requested": doc["model"]} if isinstance(doc.get("model"), str) else mapping("model")
        spec.model.requested = value(model.get("requested"), "model.requested")
        spec.model.provider = value(model.get("provider"), "model.provider")
        if spec.model.provider.is_known:
            spec.model.provider.value = spec.model.provider.value.lower()
        spec.model.resolved = value(model.get("resolved"), "model.resolved")
        parameters = model.get("parameters")
        spec.model.parameters = scrub(parameters, "model.parameters") if isinstance(parameters, dict) else {}

        instructions = doc.get("instructions")
        if isinstance(instructions, dict) and _HASH.match(str(instructions.get("hash", ""))):
            spec.instructions = Instructions(
                hash=instructions["hash"], provenance=Provenance.ATTESTED, basis=basis("instructions.hash")
            )
        elif isinstance(instructions, str) and instructions and instructions != UNKNOWN:
            cleaned = text(instructions, "instructions")
            spec.instructions = Instructions(
                hash=sha256_text(cleaned),
                text=None if options.hash_only else cleaned,
                provenance=Provenance.ATTESTED,
                basis=basis("instructions"),
            )
        else:
            if instructions not in (None, UNKNOWN):
                warn("instructions: expected text or {hash: sha256:...}; recorded as unknown")
            spec.instructions = Instructions(basis=absent())

        for index, tool in enumerate(items(doc.get("tools"), "tools")):
            where = f"tools[{index}]"
            if isinstance(tool, str):
                tool = {"ref": tool}
            if not isinstance(tool, dict) or not (tool.get("ref") or tool.get("name")):
                warn(f"{where}: a tool needs a ref or a name; skipped")
                continue
            ref = qualify(str(tool.get("ref") or tool["name"]), "tool", platform)
            spec.tools.append(ToolUse(
                ref=ref,
                name=tool.get("name"),
                description=text(tool.get("description"), f"{where}.description"),
                input_schema=scrub(tool["input_schema"], f"{where}.input_schema") if tool.get("input_schema") else None,
                output_schema=scrub(tool["output_schema"], f"{where}.output_schema") if tool.get("output_schema") else None,
                scope=value(tool.get("scope"), f"{where}.scope", Scope),
                reversible=value(tool.get("reversible"), f"{where}.reversible", bool),
                requires_confirmation=value(tool.get("requires_confirmation"), f"{where}.requires_confirmation", bool),
            ))
            entity(EntityKind.TOOL, ref, tool.get("name"))

        for index, item in enumerate(items(doc.get("knowledge"), "knowledge")):
            where = f"knowledge[{index}]"
            if isinstance(item, str):
                item = {"ref": item}
            if not isinstance(item, dict) or not (item.get("ref") or item.get("name")):
                warn(f"{where}: a knowledge source needs a ref or a name; skipped")
                continue
            ref = qualify(str(item.get("ref") or item["name"]), "knowledge", platform)
            spec.knowledge.append(KnowledgeUse(
                ref=ref,
                name=item.get("name"),
                description=text(item.get("description"), f"{where}.description"),
                type=item.get("type"),
                scope=value(item.get("scope"), f"{where}.scope", Scope),
            ))
            entity(EntityKind.KNOWLEDGE, ref, item.get("name"))

        permissions = mapping("permissions")
        spec.permissions = Permissions(
            identity=value(permissions.get("identity"), "permissions.identity", IdentityKind),
            principal=value(permissions.get("principal"), "permissions.principal"),
        )
        for index, grant in enumerate(items(permissions.get("grants"), "permissions.grants")):
            where = f"permissions.grants[{index}]"
            try:
                target = qualify(str(grant["target"]), "other", platform)
                spec.permissions.grants.append(Grant(
                    target=target, privilege=str(grant["privilege"]), scope=Scope(grant["scope"]),
                    provenance=Provenance.ATTESTED, basis=basis(where),
                ))
            except (KeyError, TypeError, ValueError):
                warn(f"{where}: a grant needs a target, a privilege and a scope ({', '.join(s.value for s in Scope)}); skipped")
                continue
            entity(EntityKind.OTHER, target)
        for index, rule in enumerate(items(permissions.get("access"), "permissions.access")):
            where = f"permissions.access[{index}]"
            if not isinstance(rule, dict) or not rule.get("principal") or not rule.get("level"):
                warn(f"{where}: an access rule needs a principal and a level; skipped")
                continue
            principal_type = rule.get("principal_type") or UNKNOWN
            if principal_type not in PRINCIPAL_TYPES:
                warn(f"{where}.principal_type: {principal_type!r} is not one of {', '.join(PRINCIPAL_TYPES)}; recorded as unknown")
                principal_type = UNKNOWN
            spec.permissions.access.append(
                AccessRule(principal=str(rule["principal"]), principal_type=principal_type, level=str(rule["level"]))
            )

        for key, block in mapping("extensions").items():
            spec.extensions[str(key)] = scrub(block if isinstance(block, dict) else {"value": block}, f"extensions.{key}")
        unexpected = {k: v for k, v in doc.items() if k not in TOP_LEVEL_KEYS}
        if unexpected:
            warn(f"unexpected top-level key(s) {', '.join(sorted(map(str, unexpected)))}; kept under extensions.{FORMAT}")
            spec.extensions.setdefault(FORMAT, {}).update(scrub(unexpected, f"extensions.{FORMAT}"))

        record = build_record(
            agent=agent,
            platform=platform,
            source=Source(
                format=FORMAT,
                artifact=artifact,
                detected_by=f"hand-written definition (marshal: {KIND})" + (f", {attestation}" if attestation else ""),
                commit=options.commit,
                pipeline_run=options.pipeline_run,
            ),
            spec=spec,
            bindings=scrub(mapping("bindings"), "bindings"),
        )
        result.records.append(record)
        result.entities = sorted(entities.values(), key=lambda e: e.ref)
        return result

