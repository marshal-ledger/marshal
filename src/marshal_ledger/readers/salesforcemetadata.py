"""Reader for Agentforce agents as Salesforce metadata (legacy Agent Builder).

Agents built in the legacy Agentforce Builder exist only as metadata, retrieved
with ``sf project retrieve start --metadata Agent:<Name>``:

- ``bots/<Name>/<Name>.bot-meta.xml``: the agent's label, type and agent user
- ``genAiPlannerBundles/<Name>/``: the planner, with its topics and actions
- ``genAiPlugins/*.genAiPlugin-meta.xml``: topics referenced by the planner
- ``genAiFunctions/<Name>/``: actions, with their input and output schemas

The agent id is the planner's developer name, the same as an Agent Script
``config.developer_name``, so an agent keeps its identity when it moves to the
new builder. Legacy agents keep instructions per topic; the record's
instructions are composed from them and marked derived.

Mapping rules are documented in ``docs/formats/agentforce.md``.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Optional

from ..hashing import sha256_text
from ..model import (
    AgentSpec,
    Entity,
    EntityKind,
    IdentityKind,
    Instructions,
    Provenance,
    Source,
    ToolUse,
    Value,
)
from ..redact import redact
from .base import ReadOptions, ReadResult, Reader, build_record

FORMAT = "salesforce-metadata"
_NS = "{http://soap.sforce.com/2006/04/metadata}"
_TARGETS = {"apex": "apex", "flow": "flow", "generatePromptResponse": "prompt", "standardInvocableAction": "standard",
            "prompt": "prompt"}
_SERVICE_TYPES = {"EinsteinServiceAgent", "AgentforceServiceAgent", "ExternalCopilot"}
_EMPLOYEE_TYPES = {"AgentforceEmployeeAgent", "EinsteinCopilot", "InternalCopilot"}
_LIGHTNING_TYPES = {
    "lightning__textType": "string", "lightning__richTextType": "string", "lightning__dateType": "string",
    "lightning__dateTimeStringType": "string", "lightning__numberType": "number", "lightning__integerType": "integer",
    "lightning__booleanType": "boolean", "lightning__objectType": "object", "lightning__recordInfoType": "object",
    "lightning__listType": "array",
}


def _xml(path: Path) -> Optional[ET.Element]:
    try:
        return ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None


def _text(element: Optional[ET.Element], tag: str) -> Optional[str]:
    if element is None:
        return None
    child = element.find(f"{_NS}{tag}")
    return child.text.strip() if child is not None and child.text else None


def _all(element: ET.Element, path: str) -> list[ET.Element]:
    return element.findall("/".join(f"{_NS}{p}" for p in path.split("/")))


def _schema(path: Path) -> Optional[dict[str, Any]]:
    """A Salesforce action schema as plain JSON Schema: Lightning annotations become types."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    props = {}
    for name, prop in (data.get("properties") or {}).items():
        item: dict[str, Any] = {}
        kind = _LIGHTNING_TYPES.get(str(prop.get("lightning:type", "")))
        if kind:
            item["type"] = kind
        if prop.get("description"):
            item["description"] = prop["description"]
        props[name] = item
    if not props:
        return None
    schema: dict[str, Any] = {"type": "object", "properties": props}
    if data.get("required"):
        schema["required"] = list(data["required"])
    return schema


def _find(roots: list[Path], folder: str, name: str, suffixes: tuple[str, ...]) -> Optional[Path]:
    """A metadata file by name: in the planner's own source folder first, then in other packages."""
    for base in roots:
        directory = base / folder
        for suffix in suffixes:
            for candidate in (directory / name / f"{name}{suffix}", directory / f"{name}{suffix}"):
                if candidate.is_file():
                    return candidate
    return None


class SalesforceMetadataReader(Reader):
    format = FORMAT
    platform = "agentforce"

    @classmethod
    def detect(cls, path: Path) -> bool:
        return path.is_dir() and any(True for _ in path.rglob("genAiPlannerBundles"))

    def read(self, path: Path, options: ReadOptions | None = None) -> ReadResult:
        options = options or ReadOptions()
        result = ReadResult()
        entities: dict[str, Entity] = {}
        for directory in sorted(path.rglob("genAiPlannerBundles")):
            if not directory.is_dir():
                continue
            source_root = directory.parent
            for bundle in sorted(directory.iterdir()):
                planner_file = next((f for f in (bundle.glob("*.genAiPlannerBundle*") if bundle.is_dir() else [])), None)
                if planner_file is None and bundle.is_file() and ".genAiPlannerBundle" in bundle.name:
                    planner_file = bundle
                if planner_file is None:
                    continue
                roots = [source_root] + sorted({d.parent for d in path.rglob("genAi*") if d.is_dir()} - {source_root})
                roots += sorted({d.parent for d in path.rglob("bots") if d.is_dir()} - set(roots))
                record = self._planner(planner_file, roots, path, options, result, entities)
                if record is not None:
                    result.records.append(record)
        result.entities = sorted(entities.values(), key=lambda e: e.ref)
        return result

    def _planner(self, planner_file, roots, root, options, result, entities):
        planner = _xml(planner_file)
        artifact = planner_file.relative_to(root).as_posix()
        if planner is None:
            result.warnings.append(f"{artifact}: not parsed")
            return None
        name = planner_file.name.split(".")[0]
        bot_file = _find(roots, "bots", name, (".bot-meta.xml",))
        bot = _xml(bot_file) if bot_file else None

        spec = AgentSpec()
        extension: dict[str, Any] = {}
        label = _text(bot, "label") or (_text(bot.find(f"{_NS}botMlDomain"), "label") if bot is not None else None) \
            or _text(planner, "masterLabel")
        if label:
            spec.display_name = Value[str].declared(label, "Bot.label")
        description = _text(planner, "description") or _text(bot, "description")
        if description:
            spec.description = Value[str].declared(redact(description)[0], "GenAiPlannerBundle.description")
        spec.model.requested = Value[str].unknown("chosen by the Agentforce reasoning engine")

        agent_type = _text(bot, "agentType")
        bot_user = _text(bot, "botUser")
        if agent_type:
            extension["agent_type"] = agent_type
        if agent_type in _EMPLOYEE_TYPES:
            spec.permissions.identity = Value[IdentityKind].derived(IdentityKind.ON_BEHALF_OF_USER, f"agent type {agent_type}")
        elif agent_type in _SERVICE_TYPES or bot_user:
            spec.permissions.identity = Value[IdentityKind].declared(IdentityKind.SERVICE_PRINCIPAL, "Bot.botUser")
            if bot_user and "%%" not in bot_user:
                spec.permissions.principal = Value[str].declared(bot_user, "Bot.botUser")
            else:
                spec.permissions.principal = Value[str].unknown("the agent user is set per org at deploy time")
        if bot is not None and _text(bot, "defaultOutboundFlow"):
            extension["default_outbound_flow"] = _text(bot, "defaultOutboundFlow")

        topics: dict[str, Any] = {}
        function_names: list[str] = [f.text.strip() for f in _all(planner, "genAiFunctions/genAiFunctionName") if f.text]
        for plugin in _all(planner, "genAiPlugins"):
            customized = plugin.find(f"{_NS}genAiCustomizedPlugin")
            reference = _text(plugin, "genAiPluginName")
            element = customized
            if element is None and reference:
                plugin_file = _find(roots, "genAiPlugins", reference, (".genAiPlugin-meta.xml", ".genAiPlugin"))
                element = _xml(plugin_file) if plugin_file else None
                if element is None:
                    topics[reference] = {"source": "not in the retrieved metadata"}
                    continue
            if element is None:
                continue
            topic = _text(element, "developerName") or _text(element, "masterLabel") or reference or "topic"
            names = [f.text.strip() for f in _all(element, "genAiFunctions/functionName") if f.text]
            function_names += names
            topics[topic] = {
                "description": _text(element, "description"),
                "scope": _text(element, "scope"),
                "instructions": [_text(i, "description") for i in _all(element, "genAiPluginInstructions") if _text(i, "description")],
                "actions": names,
                "can_escalate": _text(element, "canEscalate") == "true",
            }
        if topics:
            extension["topics"] = topics
            parts = []
            for topic, body in topics.items():
                lines = [f"Topic: {topic}"]
                if body.get("description"):
                    lines.append(f"Classification: {body['description']}")
                if body.get("scope"):
                    lines.append(f"Scope: {body['scope']}")
                lines += [f"- {i}" for i in body.get("instructions") or []]
                parts.append("\n".join(lines))
            text, count = redact("\n\n".join(parts))
            if count:
                result.warnings.append(f"{artifact}: topic instructions: redacted {count} credential-like value(s)")
            spec.instructions = Instructions(
                hash=sha256_text(text), text=None if options.hash_only else text, provenance=Provenance.DERIVED,
                basis="composed from the topics' classification, scope and instructions",
            )

        for function in dict.fromkeys(function_names):
            use = self._function(function, roots)
            if all(t.ref != use.ref for t in spec.tools):
                spec.tools.append(use)
                entities.setdefault(use.ref, Entity(ref=use.ref, kind=EntityKind.TOOL, platform="agentforce",
                                                    identifier=use.ref.split("/", 1)[1], display_name=use.name))
        spec.extensions[FORMAT] = extension
        return build_record(
            agent=name,
            platform="agentforce",
            source=Source(format=FORMAT, artifact=artifact, detected_by="GenAiPlannerBundle",
                          commit=options.commit, pipeline_run=options.pipeline_run),
            spec=spec,
        )

    def _function(self, name: str, roots: list[Path]) -> ToolUse:
        function_file = _find(roots, "genAiFunctions", name, (".genAiFunction-meta.xml", ".genAiFunction"))
        element = _xml(function_file) if function_file else None
        if element is None:
            # A standard action that ships with Agentforce, referenced by API name.
            return ToolUse(ref=f"tool/salesforce.function.{name}", name=name)
        target_type = _TARGETS.get(_text(element, "invocationTargetType") or "", _text(element, "invocationTargetType") or "function")
        target = _text(element, "invocationTarget") or name
        use = ToolUse(
            ref=f"tool/salesforce.{target_type}.{target}",
            name=name,
            description=_text(element, "description"),
            input_schema=_schema(function_file.parent / "input" / "schema.json"),
            output_schema=_schema(function_file.parent / "output" / "schema.json"),
        )
        confirmation = _text(element, "isConfirmationRequired")
        if confirmation in ("true", "false"):
            use.requires_confirmation = Value[bool].declared(confirmation == "true", f"GenAiFunction {name}")
        return use
