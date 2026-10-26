"""Reader for Microsoft Copilot Studio agents: ``.mcs.yml`` workspaces and solutions.

A workspace is what the Copilot Studio extension for Visual Studio Code clones:
``agent.mcs.yml`` (instructions, model, capabilities), ``settings.mcs.yml``
(schema name, access and authentication), ``actions/`` (tools), ``knowledge/``,
``topics/``, ``trigger/``, ``agents/`` (sub-agents) and
``connectionreferences.mcs.yml``.

A solution is what ``pac solution export`` produces, as a zip or unpacked with
``pac solution unpack``: ``bots/<schema>/bot.xml`` and ``configuration.json``
(the settings), and one ``botcomponents/<schema>/`` folder per component, whose
``data`` file holds the same YAML a workspace file does. Components are told
apart by that YAML's ``kind``, so both layouts go through one mapping.

Each action's connection mode says whose credentials it uses: ``Invoker``, the
end user's, or ``Maker``, the credentials its author configured (the schema
default). The agent's identity is derived from these modes.

Mapping rules are documented in ``docs/formats/copilot-studio.md``.
"""

from __future__ import annotations

import functools
import json
import re
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Optional

import yaml

from ..hashing import sha256_text
from ..model import (
    MARSHAL_NAMESPACE,
    AccessRule,
    AgentSpec,
    Entity,
    EntityKind,
    IdentityKind,
    Instructions,
    KnowledgeUse,
    Provenance,
    Scope,
    Source,
    ToolUse,
    Value,
)
from ..redact import redact
from .base import ReadOptions, ReadResult, Reader, build_record, jsonable

FORMAT = "copilot-studio"
AGENT_FILES = ("agent.mcs.yml", "agent.mcs.yaml")
_NAME_COMMENT = re.compile(r"^#\s*Name:\s*(.+?)\s*$", re.MULTILINE)
_MARSHAL_COMMENT = re.compile(r"^#\s*" + re.escape(MARSHAL_NAMESPACE) + r":\s*(\{.*\})\s*$", re.MULTILINE)
_FENCE = re.compile(r"^\s*```[^\n]*\n|\n?```\s*$")
# Copilot Studio writes keys such as `@odata.type` unquoted, which YAML reserves; quote them to parse.
_AT_KEY = re.compile(r"^(\s*(?:-\s+)?)(@[^\s:]+):", re.MULTILINE)
_READ_METHODS = {"GET", "HEAD", "OPTIONS"}
_ACCESS = {
    "Any": ("anyone in the tenant", "group"),
    "AnyMultiTenant": ("anyone in any tenant", "group"),
    "ChatbotReaders": ("users the agent is shared with", "group"),
    "GroupMembership": ("members of the configured security groups", "group"),
}
# Copilot Studio's ModelProvider enum, to the ledger's provider names.
PROVIDERS = {"OpenAI": "openai", "AzureOpenAI": "azure-openai", "Anthropic": "anthropic", "GoogleVertexAI": "google"}
_KNOWLEDGE_TYPES = {
    "SharePointSearchSource": "sharepoint",
    "PublicSiteSearchSource": "public_website",
    "DataverseStructuredSearchSource": "dataverse",
    "GraphConnectorSearchSource": "graph_connector",
    "AzureAISearchSource": "azure_ai_search",
}
_CAPABILITIES = {
    "webBrowsing": ("tool/copilot-studio.web_search", Scope.READ, "searches the public web"),
    "codeInterpreter": ("tool/copilot-studio.code_interpreter", Scope.EXECUTE, "runs generated code"),
}
# bot.xml stores these choice columns as integers. The labels are the YAML names a workspace uses,
# in the order of the Dataverse bot table's option values.
_BOT_CHOICES = {
    "authenticationmode": ("authenticationMode", ("Unspecified", "None", "Integrated", "CustomAzureActiveDirectory", "GenericOAuth2")),
    "authenticationtrigger": ("authenticationTrigger", ("AsNeeded", "Always")),
    "accesscontrolpolicy": ("accessControlPolicy", ("Any", "ChatbotReaders", "GroupMembership", "AnyMultiTenant")),
}
# A solution component's YAML kind, to the workspace folder that holds the same kind.
_COMPONENT_FOLDERS = {
    "TaskDialog": "actions",
    "AgentDialog": "agents",
    "SkillDefinition": "skills",
    "AdaptiveDialog": "topics",
    "KnowledgeSourceConfiguration": "knowledge",
    "ExternalTriggerConfiguration": "trigger",
}
_FILE_ATTACHMENT = "14"  # botcomponent componenttype: Bot File Attachment
_ZIP_MEMBER_PREFIXES = ("bots/", "botcomponents/")
_ZIP_LIMIT = 256 * 1024 * 1024


def load(path: Path) -> tuple[Any, Optional[str]]:
    """Parse a ``.mcs.yml`` file, tolerating a Markdown code fence and unquoted ``@`` keys. Returns (data, name comment)."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, None
    name = _NAME_COMMENT.search(text)
    unfenced = _FENCE.sub("", text)
    for candidate in (text, unfenced, _AT_KEY.sub(r'\1"\2":', unfenced)):
        try:
            return jsonable(yaml.safe_load(candidate)), name.group(1) if name else None
        except yaml.YAMLError:
            continue
    return None, name.group(1) if name else None


def connector_name(reference: str, connectors: dict[str, str]) -> str:
    """The connector API (``shared_office365``) behind a connection reference logical name."""
    if reference in connectors:
        return connectors[reference].rsplit("/", 1)[-1]
    parts = reference.split(".")
    return parts[1] if len(parts) >= 2 else reference


def _walk(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _schema(inputs: list[Any]) -> Optional[dict[str, Any]]:
    """Inputs the model fills in (``AutomaticTaskInput``) as a JSON Schema."""
    props, required = {}, []
    for item in inputs or []:
        if isinstance(item, dict) and item.get("kind") == "AutomaticTaskInput" and item.get("propertyName"):
            name = str(item["propertyName"])
            props[name] = {"type": "string", **({"description": item["description"]} if item.get("description") else {})}
            if "defaultValue" not in item:
                required.append(name)
    if not props:
        return None
    return {"type": "object", "properties": props, **({"required": required} if required else {})}


@functools.cache
def _operations() -> dict[tuple[str, str], tuple[str, str]]:
    """(connector, operation id) → (effect, documentation URL), from powerplatform-operations.yaml."""
    path = resources.files("marshal_ledger") / "readers" / "powerplatform-operations.yaml"
    table: dict[tuple[str, str], tuple[str, str]] = {}
    for connector, entry in (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).items():
        for effect in ("read", "write", "irreversible"):
            for operation in entry.get(effect) or []:
                table[(connector, str(operation))] = (effect, str(entry["source"]))
    return table


def derive_effect(use: ToolUse) -> None:
    """Scope and reversibility of a standard connector operation, from Microsoft's connector reference."""
    if not use.ref.startswith("tool/powerplatform.") or use.scope.is_known:
        return
    connector, _, operation = use.ref.removeprefix("tool/powerplatform.").partition(".")
    found = _operations().get((connector, operation))
    if found is None:
        return
    effect, source = found
    if effect == "read":
        use.scope = Value[Scope].derived(Scope.READ, f"{operation} reads data ({source})")
        use.reversible = Value[bool].derived(True, "reads have no side effects")
    else:
        use.scope = Value[Scope].derived(Scope.WRITE, f"{operation} changes data ({source})")
        if effect == "irreversible":
            use.reversible = Value[bool].derived(False, f"{operation} sends a message, which can't be recalled")


@dataclass
class _Part:
    """One component: a workspace file, or a solution component folder."""

    key: str
    where: str
    data: Any = None
    name: Optional[str] = None
    file: bool = False
    """An uploaded knowledge file."""


@dataclass
class _Agent:
    """One agent's definition, gathered from either layout."""

    agent: dict[str, Any]
    agent_where: str
    agent_name: Optional[str]
    settings: dict[str, Any]
    settings_where: str
    connectors: dict[str, str]
    artifact: str
    detected_by: str
    default_id: str
    description: Optional[str] = None
    description_where: Optional[str] = None
    marshal: dict[str, Any] = field(default_factory=dict)
    parts: dict[str, list[_Part]] = field(default_factory=dict)


def _xml(path: Path) -> Optional[ET.Element]:
    try:
        return ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None


def _text(element: Optional[ET.Element], tag: str) -> Optional[str]:
    child = element.find(tag) if element is not None else None
    return child.text.strip() if child is not None and child.text and child.text.strip() else None


def _solution_connectors(solution: Path) -> dict[str, str]:
    """Connection reference logical names to connector ids, from the solution's customizations."""
    for candidate in (solution / "Other" / "Customizations.xml", solution / "customizations.xml"):
        root = _xml(candidate) if candidate.is_file() else None
        if root is not None:
            return {
                str(ref.get("connectionreferencelogicalname")): _text(ref, "connectorid") or ""
                for ref in root.iter("connectionreference") if ref.get("connectionreferencelogicalname")
            }
    return {}


def _component_key(schema: str, bot: str) -> str:
    """``<bot>.topic.Greeting`` → ``Greeting``: the name a workspace gives the same component's file."""
    rest = schema[len(bot) + 1:] if schema.startswith(bot + ".") else schema
    return rest.split(".", 1)[1] if "." in rest else rest


def _solution_agents(solution: Path, label: str) -> Iterator[_Agent]:
    prefix = f"{label}/" if label else ""
    connectors = _solution_connectors(solution)
    components: dict[str, list[tuple[str, ET.Element, Path]]] = {}
    for xml_path in sorted((solution / "botcomponents").glob("*/botcomponent.xml")):
        element = _xml(xml_path)
        if element is None:
            continue
        schema = element.get("schemaname") or xml_path.parent.name
        parent = _text(element.find("parentbotid"), "schemaname") or schema.split(".", 1)[0]
        components.setdefault(parent, []).append((schema, element, xml_path.parent))

    for bot_xml in sorted((solution / "bots").glob("*/bot.xml")):
        bot = _xml(bot_xml)
        if bot is None:
            continue
        folder = bot_xml.parent
        schema = bot.get("schemaname") or folder.name
        settings: dict[str, Any] = {"schemaName": schema}
        if _text(bot, "name"):
            settings["displayName"] = _text(bot, "name")
        for tag, (key, labels) in _BOT_CHOICES.items():
            raw = _text(bot, tag)
            if raw is not None and raw.isdigit() and int(raw) < len(labels):
                settings[key] = labels[int(raw)]
        try:
            config = json.loads((folder / "configuration.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            config = None
        if isinstance(config, dict):
            settings["configuration"] = {k: v for k, v in config.items() if k != "$kind"}
        language = _text(bot, "language")
        if language and language.isdigit():
            settings["language"] = int(language)

        default_gpt = ((config or {}).get("gPTSettings") or {}).get("defaultSchemaName") if isinstance(config, dict) else None
        view = _Agent(
            agent={}, agent_where=f"{prefix}bots/{folder.name}/bot.xml", agent_name=None,
            settings=settings, settings_where=f"{prefix}bots/{folder.name}/bot.xml", connectors=connectors,
            artifact=f"{prefix}bots/{folder.name}/bot.xml", detected_by="Copilot Studio solution", default_id=schema,
        )
        gpts = []
        for comp_schema, element, comp_folder in sorted(components.get(schema, []), key=lambda c: c[0]):
            rel = f"{prefix}botcomponents/{comp_folder.name}"
            data, _ = load(comp_folder / "data")
            kind = data.get("kind") if isinstance(data, dict) else None
            key = _component_key(comp_schema, schema)
            if kind == "GptComponentMetadata":
                gpts.append((comp_schema, element, data, rel))
            elif _text(element, "componenttype") == _FILE_ATTACHMENT and element.find("parentbotcomponentid") is None:
                view.parts.setdefault("knowledge", []).append(
                    _Part(key=key, where=f"{rel}/botcomponent.xml", name=_text(element, "name"), file=True))
            elif kind in _COMPONENT_FOLDERS:
                view.parts.setdefault(_COMPONENT_FOLDERS[kind], []).append(
                    _Part(key=key, where=f"{rel}/data", data=data, name=_text(element, "name")))
        chosen = next((g for g in gpts if g[0] == default_gpt), gpts[0] if gpts else None)
        if chosen:
            _, element, data, rel = chosen
            view.agent, view.agent_where, view.agent_name = data, f"{rel}/data", _text(element, "name")
            if _text(element, "description"):
                view.description, view.description_where = _text(element, "description"), f"{rel}/botcomponent.xml"
        yield view


def _solution_zip(path: Path) -> bool:
    if not (path.is_file() and path.suffix.lower() == ".zip" and zipfile.is_zipfile(path)):
        return False
    try:
        with zipfile.ZipFile(path) as archive:
            return any(re.fullmatch(r"bots/[^/]+/bot\.xml", n.replace("\\", "/")) for n in archive.namelist())
    except (OSError, zipfile.BadZipFile):
        return False


def _extract(path: Path, dest: Path) -> None:
    """Extract the agent parts of a solution zip, refusing unsafe paths and oversized content."""
    remaining = _ZIP_LIMIT
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            parts = PurePosixPath(name).parts
            if info.is_dir() or name.startswith("/") or ".." in parts or not parts or ":" in parts[0]:
                continue
            if not name.startswith(_ZIP_MEMBER_PREFIXES) and name.lower() != "customizations.xml":
                continue
            with archive.open(info) as source:
                content = source.read(remaining + 1)
            remaining -= len(content)
            if remaining < 0:
                raise ValueError(f"{path.name}: agent content exceeds {_ZIP_LIMIT // (1024 * 1024)} MB")
            target = dest.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)


class CopilotStudioReader(Reader):
    format = FORMAT
    platform = FORMAT

    @classmethod
    def detect(cls, path: Path) -> bool:
        return _solution_zip(path) or any(True for _ in cls._workspaces(path)) or any(True for _ in cls._solutions(path))

    @staticmethod
    def _workspaces(path: Path) -> Iterator[Path]:
        if path.is_file():
            if path.name in AGENT_FILES:
                yield path.parent
            return
        if not path.is_dir():
            return
        for name in AGENT_FILES:
            if (path / name).is_file():
                yield path
                return
        for candidate in sorted(path.rglob("agent.mcs.y*ml")):
            if candidate.name in AGENT_FILES and candidate.parent.parent.name != "agents" and ".cache" not in candidate.parts:
                yield candidate.parent

    @staticmethod
    def _solutions(path: Path) -> Iterator[Path]:
        """Unpacked solution roots: folders whose ``bots/<schema>/bot.xml`` defines an agent."""
        if not path.is_dir():
            return
        if any((path / "bots").glob("*/bot.xml")):
            yield path
            return
        seen = set()
        for bot_xml in sorted(path.rglob("bot.xml")):
            root = bot_xml.parent.parent.parent
            if bot_xml.parent.parent.name == "bots" and ".cache" not in bot_xml.parts and root not in seen:
                seen.add(root)
                yield root

    def read(self, path: Path, options: ReadOptions | None = None) -> ReadResult:
        options = options or ReadOptions()
        result = ReadResult()
        entities: dict[str, Entity] = {}
        if _solution_zip(path):
            with tempfile.TemporaryDirectory() as tmp:
                _extract(path, Path(tmp))
                for view in _solution_agents(Path(tmp), path.name):
                    self._agent(view, options, result, entities)
        else:
            root = path if path.is_dir() else path.parent
            for workspace in self._workspaces(path):
                self._agent(self._workspace(workspace, root), options, result, entities)
            for solution in self._solutions(path):
                label = solution.relative_to(root).as_posix() if solution != root else ""
                for view in _solution_agents(solution, label):
                    self._agent(view, options, result, entities)
        result.entities = sorted(entities.values(), key=lambda e: e.ref)
        return result

    def _workspace(self, workspace: Path, root: Path) -> _Agent:
        rel = workspace.relative_to(root).as_posix() if workspace != root else ""
        prefix = f"{rel}/" if rel else ""
        agent_file = next(workspace / n for n in AGENT_FILES if (workspace / n).is_file())
        agent_data, agent_comment = load(agent_file)
        settings, _ = load(workspace / "settings.mcs.yml") if (workspace / "settings.mcs.yml").is_file() else (None, None)
        refs_data, _ = load(workspace / "connectionreferences.mcs.yml") if (workspace / "connectionreferences.mcs.yml").is_file() else (None, None)
        meta = (agent_data or {}).get("mcs.metadata") or {} if isinstance(agent_data, dict) else {}
        view = _Agent(
            agent=agent_data if isinstance(agent_data, dict) else {},
            agent_where=f"{prefix}{agent_file.name}", agent_name=meta.get("componentName") or agent_comment,
            settings=settings if isinstance(settings, dict) else {}, settings_where=f"{prefix}settings.mcs.yml",
            connectors={
                str(c.get("connectionReferenceLogicalName")): str(c.get("connectorId", ""))
                for c in ((refs_data or {}).get("connectionReferences") or []) if isinstance(c, dict)
            },
            artifact=f"{prefix}{agent_file.name}", detected_by="Copilot Studio agent workspace", default_id=workspace.name,
            description=meta.get("description"), description_where=f"{prefix}{agent_file.name}",
            marshal=self._marshal(workspace / "settings.mcs.yml"),
        )
        folders = {"actions": "*.mcs.y*ml", "skills": "*.mcs.y*ml", "topics": "*.mcs.y*ml", "trigger": "*.mcs.y*ml"}
        for folder, pattern in folders.items():
            for file in sorted((workspace / folder).glob(pattern)) if (workspace / folder).is_dir() else []:
                data, comment = load(file)
                view.parts.setdefault(folder, []).append(
                    _Part(key=file.name.split(".mcs")[0], where=f"{prefix}{folder}/{file.name}", data=data, name=comment))
        for file in sorted((workspace / "agents").glob("*/agent.mcs.y*ml")) if (workspace / "agents").is_dir() else []:
            data, comment = load(file)
            view.parts.setdefault("agents", []).append(
                _Part(key=file.parent.name, where=f"{prefix}agents/{file.parent.name}/{file.name}", data=data, name=comment))
        for file in sorted((workspace / "knowledge").rglob("*.mcs.y*ml")) if (workspace / "knowledge").is_dir() else []:
            data, comment = load(file)
            view.parts.setdefault("knowledge", []).append(_Part(
                key=file.name.split(".mcs")[0], where=f"{prefix}{file.relative_to(workspace).as_posix()}",
                data=data, name=comment, file=file.parent.name == "files"))
        return view

    def _agent(self, view: _Agent, options: ReadOptions, result: ReadResult, entities: dict[str, Entity]) -> None:
        agent_data, settings, connectors = view.agent, view.settings, view.connectors
        schema_name = str(settings.get("schemaName") or view.default_id)
        agent_id = str(view.marshal.get("agent") or schema_name)
        name = settings.get("displayName") or agent_data.get("displayName") or view.agent_name or settings.get("name")

        def entity(kind: EntityKind, ref: str, display: Optional[str] = None) -> None:
            entities.setdefault(ref, Entity(ref=ref, kind=kind, platform=FORMAT, identifier=ref.split("/", 1)[1], display_name=display))

        spec = AgentSpec()
        extension: dict[str, Any] = {}
        bindings: dict[str, Any] = {}
        if name:
            spec.display_name = Value[str].declared(str(name), view.settings_where)
        if view.description:
            spec.description = Value[str].declared(str(view.description), str(view.description_where))
        self._instructions(spec, agent_data.get("instructions"), view.agent_where, options, result)

        model = (agent_data.get("aISettings") or {}).get("model") or {}
        if model.get("modelNameHint"):
            hint = str(model["modelNameHint"])
            spec.model.requested = Value[str].declared(hint, f"{view.agent_where}#aISettings.model.modelNameHint")
            if model.get("provider"):
                provider = str(model["provider"])
                spec.model.provider = Value[str].declared(PROVIDERS.get(provider, provider.lower()), f"{view.agent_where}#aISettings.model.provider")
            elif hint.lower().startswith("gpt"):
                spec.model.provider = Value[str].derived("azure-openai", "Copilot Studio serves GPT models through Azure OpenAI")
            elif hint.lower().startswith("claude"):
                spec.model.provider = Value[str].derived("anthropic", "Claude model name")
        else:
            spec.model.requested = Value[str].unknown("Copilot Studio's default model")
        for key in ("conversationStarters", "gptCapabilities", "responseInstructions", "defaultResponseMode"):
            if agent_data.get(key) not in (None, [], {}):
                extension[key] = agent_data[key]
        ai_settings = {k: v for k, v in (agent_data.get("aISettings") or {}).items() if k != "model"}
        if ai_settings:
            extension["aISettings"] = ai_settings

        for capability, enabled in (agent_data.get("gptCapabilities") or {}).items():
            if enabled is True and capability in _CAPABILITIES:
                ref, scope, rule = _CAPABILITIES[capability]
                spec.tools.append(ToolUse(ref=ref, name=capability, scope=Value[Scope].derived(scope, f"{capability} {rule}")))
                entity(EntityKind.TOOL, ref, capability)

        # Actions (tools)
        modes: list[str] = []
        flows = False  # a flow's connections are defined in the flow, outside the agent definition
        action_extras: dict[str, Any] = {}
        for part in view.parts.get("actions", []):
            if not isinstance(part.data, dict):
                result.warnings.append(f"{part.where}: not parsed")
                continue
            use, mode, extra = self._action(part.data, part.name, part.key, part.where, connectors)
            if use is None:
                result.warnings.append(f"{part.where}: unrecognized action {((part.data.get('action') or {}).get('kind'))}")
                continue
            if mode:
                modes.append(mode)
            flows = flows or use.ref.startswith("tool/powerplatform.flow.")
            spec.tools.append(use)
            entity(EntityKind.AGENT if use.ref.startswith("agent/") else EntityKind.TOOL, use.ref, use.name)
            action_extras[part.key] = extra

        # Sub-agents: separate records, called by this agent.
        sub_records = []
        for part in view.parts.get("agents", []):
            sub_record = self._sub_agent(part, agent_id, options, result)
            if sub_record is not None:
                sub_records.append(sub_record)
                ref = f"agent/{sub_record.agent}"
                spec.tools.append(ToolUse(ref=ref, name=sub_record.spec.display_name.value, description=sub_record.spec.description.value))
                entity(EntityKind.AGENT, ref, sub_record.spec.display_name.value)

        # Skills
        for part in view.parts.get("skills", []):
            if isinstance(part.data, dict) and part.data.get("kind") == "SkillDefinition":
                ref = f"tool/botframework.skill.{part.key}"
                spec.tools.append(ToolUse(ref=ref, name=part.name or part.key))
                entity(EntityKind.TOOL, ref, part.name)

        # Calls topics make: HTTP requests, connector operations and flows. A topic runs them as
        # scripted steps rather than the model choosing them, but the agent reaches the same systems.
        topics: dict[str, Any] = {}
        topic_calls: dict[str, dict[str, Any]] = {}
        for part in view.parts.get("topics", []):
            if not isinstance(part.data, dict):
                result.warnings.append(f"{part.where}: not parsed")
                continue
            trigger = (part.data.get("beginDialog") or {}).get("kind")
            topics[part.key] = {"name": part.name, "trigger": trigger} if part.name else {"trigger": trigger}
            for node in _walk(part.data):
                call = self._topic_call(node, connectors)
                if call is not None:
                    ref, mode = call
                    entry = topic_calls.setdefault(ref, {"topics": []})
                    if part.key not in entry["topics"]:
                        entry["topics"].append(part.key)
                    flows = flows or ref.startswith("tool/powerplatform.flow.")
                    if mode:
                        modes.append(mode)
                        entry["connection_mode"] = mode
                        entry["credentials"] = "end user" if mode == "Invoker" else "maker"
                    if all(t.ref != ref for t in spec.tools):
                        spec.tools.append(ToolUse(ref=ref))
                        entity(EntityKind.TOOL, ref)
                elif node.get("kind") == "HttpRequestAction" and isinstance(node.get("url"), str):
                    method = str(node.get("method") or "GET").upper()
                    ref = f"tool/http.{method}.{node['url']}"
                    if all(t.ref != ref for t in spec.tools):
                        read = method in _READ_METHODS
                        spec.tools.append(ToolUse(
                            ref=ref, name=node.get("displayName") or f"{method} {node['url']}",
                            scope=Value[Scope].derived(Scope.READ if read else Scope.WRITE, f"HTTP {method} in topic {part.where}"),
                        ))
                        entity(EntityKind.TOOL, ref)
        if topics:
            extension["topics"] = topics
        for use in spec.tools:
            derive_effect(use)
        if topic_calls:
            extension["topic_calls"] = {ref: {**e, "topics": sorted(e["topics"])} for ref, e in sorted(topic_calls.items())}

        # Knowledge
        for part in view.parts.get("knowledge", []):
            item = self._knowledge(part, agent_id)
            if item is not None:
                spec.knowledge.append(item)
                entity(EntityKind.KNOWLEDGE, item.ref, item.name)

        # Triggers: the agent acts without a user present.
        triggers = []
        for part in view.parts.get("trigger", []):
            source = (part.data or {}).get("externalTriggerSource") or {} if isinstance(part.data, dict) else {}
            reference = source.get("connectionReference")
            triggers.append({
                "name": part.name or part.key,
                **({"connector": connector_name(str(reference), connectors)} if reference else {}),
            })
            if reference:
                modes.append("Maker")
        if triggers:
            extension["triggers"] = triggers

        # Identity, from the connection modes of tools and triggers.
        if "Invoker" in modes and "Maker" in modes:
            spec.permissions.identity = Value[IdentityKind].derived(IdentityKind.MIXED, "connections use both maker and end-user credentials")
        elif flows:
            spec.permissions.identity = Value[IdentityKind].unknown(
                "the agent calls flows, whose connections are defined in the flow, not in the agent definition")
        elif modes:
            if all(m == "Invoker" for m in modes):
                spec.permissions.identity = Value[IdentityKind].derived(IdentityKind.ON_BEHALF_OF_USER, "every connection uses the end user's credentials")
            elif all(m == "Maker" for m in modes):
                spec.permissions.identity = Value[IdentityKind].derived(IdentityKind.SHARED_CREDENTIAL, "every connection uses the maker's credentials")
            else:
                spec.permissions.identity = Value[IdentityKind].derived(IdentityKind.MIXED, "connections use both maker and end-user credentials")
        else:
            spec.permissions.identity = Value[IdentityKind].unknown("the agent declares no connections")

        policy = settings.get("accessControlPolicy")
        if policy in _ACCESS:
            principal, kind = _ACCESS[policy]
            spec.permissions.access.append(AccessRule(principal=principal, principal_type=kind, level="chat"))
        if settings.get("authenticationMode") == "None":
            spec.permissions.access.append(AccessRule(principal="unauthenticated users", principal_type="unknown", level="chat"))
        for key in ("schemaName", "accessControlPolicy", "authenticationMode", "authenticationTrigger", "configuration", "language"):
            if key in settings:
                extension.setdefault("settings", {})[key] = settings[key]
        if action_extras:
            extension["actions"] = action_extras
        if connectors:
            bindings["connection_references"] = sorted(connectors)
        if settings.get("cdsBotId"):
            bindings["dataverse_bot_id"] = settings["cdsBotId"]
        spec.extensions[FORMAT] = extension

        result.records.append(build_record(
            agent=agent_id,
            platform=str(view.marshal.get("platform") or FORMAT),
            source=Source(format=FORMAT, artifact=view.artifact, detected_by=view.detected_by,
                          commit=options.commit, pipeline_run=options.pipeline_run),
            spec=spec,
            bindings=bindings,
        ))
        result.records.extend(sub_records)

    @staticmethod
    def _marshal(path: Path) -> dict[str, Any]:
        """Identity a Marshal conversion recorded in a comment, so a converted agent reads back as itself."""
        try:
            match = _MARSHAL_COMMENT.search(path.read_text(encoding="utf-8"))
            value = json.loads(match.group(1)) if match else {}
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _instructions(self, spec: AgentSpec, text: Any, where: str, options: ReadOptions, result: ReadResult) -> None:
        if isinstance(text, str) and text.strip():
            cleaned, count = redact(text)
            if count:
                result.warnings.append(f"{where}: redacted {count} credential-like value(s)")
            spec.instructions = Instructions(
                hash=sha256_text(cleaned), text=None if options.hash_only else cleaned,
                provenance=Provenance.DECLARED, basis=f"{where}#instructions",
            )

    def _action(self, data, comment, key, where, connectors):
        action = data.get("action") or {}
        kind = action.get("kind")
        name = data.get("modelDisplayName") or comment or key
        use = ToolUse(ref="", name=str(name), description=data.get("modelDescription"), input_schema=_schema(data.get("inputs")))
        if data.get("confirmation"):
            use.requires_confirmation = Value[bool].declared(True, f"{where}#confirmation")
        mode = None
        extra: dict[str, Any] = {"kind": kind}
        reference = action.get("connectionReference")
        if reference:
            mode = str((action.get("connectionProperties") or {}).get("mode") or "Maker")
            extra["connection_mode"] = mode
            extra["connector"] = connector_name(str(reference), connectors)
        if kind == "InvokeConnectorTaskAction":
            use.ref = f"tool/powerplatform.{extra['connector']}.{action.get('operationId')}"
        elif kind == "InvokeExternalAgentTaskAction":
            details = action.get("operationDetails") or {}
            if details.get("kind") == "ModelContextProtocolMetadata":
                use.ref = f"tool/mcp.powerplatform.{extra['connector']}"
                extra["operation"] = details.get("operationId")
            else:
                use.ref = f"tool/powerplatform.{extra.get('connector')}.{details.get('operationId')}"
        elif kind == "InvokeFlowTaskAction":
            use.ref = f"tool/powerplatform.flow.{action.get('flowId')}"
        elif kind in ("InvokeConnectedAgentTaskAction", "InvokeAgentTaskAction"):
            target = action.get("botSchemaName") or action.get("agentSchemaName")
            if not target:
                return None, None, extra
            use.ref = f"agent/{target}"
        elif kind == "InvokeAIPluginTaskAction":
            # A prompt: entityKey is "aiplugin.name=<plugin>,operationid=<operation>".
            fields = dict(p.split("=", 1) for p in str(action.get("entityKey") or "").split(",") if "=" in p)
            use.ref = f"tool/powerplatform.ai_plugin.{fields.get('aiplugin.name') or name}"
            if (action.get("primaryConnectionReference") or {}).get("kind") == "InvokerConnectionReference":
                mode = "Invoker"
                extra["connection_mode"] = mode
        elif kind == "InvokeAIBuilderModelTaskAction":
            use.ref = f"tool/powerplatform.ai_builder.{action.get('aIModelId') or name}"
        else:
            return None, None, extra
        if mode:
            extra["credentials"] = "end user" if mode == "Invoker" else "maker"
        extra["tool"] = use.ref
        return use, mode, extra

    @staticmethod
    def _topic_call(node: dict[str, Any], connectors: dict[str, str]) -> Optional[tuple[str, Optional[str]]]:
        """(tool ref, connection mode) for a connector or flow call in a topic, else None."""
        kind = node.get("kind")
        if kind == "InvokeConnectorAction" and node.get("connectionReference"):
            connector = connector_name(str(node["connectionReference"]), connectors)
            operation = node.get("operationId")
            mode = str((node.get("connectionProperties") or {}).get("mode") or "Maker")
            return (f"tool/powerplatform.{connector}.{operation}" if operation else f"tool/powerplatform.{connector}"), mode
        if kind == "InvokeFlowAction" and node.get("flowId"):
            return f"tool/powerplatform.flow.{node['flowId']}", None
        return None

    def _sub_agent(self, part: _Part, parent_id: str, options: ReadOptions, result: ReadResult):
        data = part.data
        if not isinstance(data, dict) or data.get("kind") != "AgentDialog":
            return None
        spec = AgentSpec()
        spec.display_name = Value[str].declared(part.name or part.key, part.where)
        description = (data.get("beginDialog") or {}).get("description")
        if description:
            spec.description = Value[str].declared(str(description), f"{part.where}#beginDialog.description")
        self._instructions(spec, (data.get("settings") or {}).get("instructions"), part.where, options, result)
        spec.model.requested = Value[str].unknown("sub-agents use the parent agent's model")
        spec.extensions[FORMAT] = {"parent": parent_id, "kind": "AgentDialog"}
        return build_record(
            agent=f"{parent_id}/{part.key}",
            platform=FORMAT,
            source=Source(format=FORMAT, artifact=part.where, detected_by="Copilot Studio sub-agent",
                          commit=options.commit, pipeline_run=options.pipeline_run),
            spec=spec,
        )

    def _knowledge(self, part: _Part, agent_id: str) -> Optional[KnowledgeUse]:
        if part.file:
            name = part.name or part.key
            return KnowledgeUse(ref=f"knowledge/copilot-studio.{agent_id}.file.{name}", name=name, type="file",
                                scope=Value[Scope].derived(Scope.READ, "uploaded knowledge files are searched"))
        data = part.data
        if not isinstance(data, dict):
            return None
        # Either a KnowledgeSourceConfiguration wrapping a source, or the source itself.
        source = data.get("source") if data.get("kind") == "KnowledgeSourceConfiguration" else data
        if not isinstance(source, dict) or not str(source.get("kind", "")).endswith("Source"):
            return None
        kind = str(source.get("kind") or "KnowledgeSource")
        site = source.get("site")
        name = part.name or part.key
        ref = f"knowledge/{site}" if isinstance(site, str) else f"knowledge/copilot-studio.{agent_id}.{name}"
        return KnowledgeUse(
            ref=ref, name=name, type=_KNOWLEDGE_TYPES.get(kind) or re.sub(r"(?<!^)(?=[A-Z])", "_", kind.removesuffix("Source")).lower(),
            scope=Value[Scope].derived(Scope.READ, f"{kind} is a search source"),
        )
