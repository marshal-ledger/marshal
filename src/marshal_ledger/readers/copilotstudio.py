"""Reader for Microsoft Copilot Studio agent definitions (``.mcs.yml`` workspaces).

A workspace is what the Copilot Studio extension for Visual Studio Code clones:
``agent.mcs.yml`` (instructions, model, capabilities), ``settings.mcs.yml``
(schema name, access and authentication), ``actions/`` (tools), ``knowledge/``,
``topics/``, ``trigger/``, ``agents/`` (sub-agents) and
``connectionreferences.mcs.yml``.

Each action's connection mode says whose credentials it uses: ``Invoker``, the
end user's, or ``Maker``, the credentials its author configured (the schema
default). The agent's identity is derived from these modes.

Mapping rules are documented in ``docs/formats/copilot-studio.md``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
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


def load(path: Path) -> tuple[Any, Optional[str]]:
    """Parse a ``.mcs.yml`` file, tolerating a surrounding Markdown code fence. Returns (data, name comment)."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, None
    name = _NAME_COMMENT.search(text)
    for candidate in (text, _FENCE.sub("", text)):
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


class CopilotStudioReader(Reader):
    format = FORMAT
    platform = FORMAT

    @classmethod
    def detect(cls, path: Path) -> bool:
        return any(True for _ in cls._workspaces(path))

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

    def read(self, path: Path, options: ReadOptions | None = None) -> ReadResult:
        options = options or ReadOptions()
        result = ReadResult()
        root = path if path.is_dir() else path.parent
        entities: dict[str, Entity] = {}
        for workspace in self._workspaces(path):
            self._workspace(workspace, root, options, result, entities)
        result.entities = sorted(entities.values(), key=lambda e: e.ref)
        return result

    def _workspace(self, workspace: Path, root: Path, options: ReadOptions, result: ReadResult, entities: dict[str, Entity]) -> None:
        rel = workspace.relative_to(root).as_posix() if workspace != root else ""
        prefix = f"{rel}/" if rel else ""
        agent_file = next(workspace / n for n in AGENT_FILES if (workspace / n).is_file())
        agent_data, agent_comment = load(agent_file)
        settings, _ = load(workspace / "settings.mcs.yml") if (workspace / "settings.mcs.yml").is_file() else (None, None)
        settings = settings if isinstance(settings, dict) else {}
        refs_data, _ = load(workspace / "connectionreferences.mcs.yml") if (workspace / "connectionreferences.mcs.yml").is_file() else (None, None)
        connectors = {
            str(c.get("connectionReferenceLogicalName")): str(c.get("connectorId", ""))
            for c in ((refs_data or {}).get("connectionReferences") or []) if isinstance(c, dict)
        }
        meta = (agent_data or {}).get("mcs.metadata") or {}
        schema_name = settings.get("schemaName") or workspace.name
        marshal = self._marshal(workspace / "settings.mcs.yml")
        agent_id = str(marshal.get("agent") or schema_name)
        name = (settings.get("displayName") or (agent_data or {}).get("displayName") or meta.get("componentName")
                or agent_comment or settings.get("name"))

        def entity(kind: EntityKind, ref: str, display: Optional[str] = None) -> None:
            entities.setdefault(ref, Entity(ref=ref, kind=kind, platform=FORMAT, identifier=ref.split("/", 1)[1], display_name=display))

        spec = AgentSpec()
        extension: dict[str, Any] = {}
        bindings: dict[str, Any] = {}
        if name:
            spec.display_name = Value[str].declared(str(name), f"{prefix}settings.mcs.yml")
        if meta.get("description"):
            spec.description = Value[str].declared(str(meta["description"]), f"{prefix}agent.mcs.yml")
        self._instructions(spec, (agent_data or {}).get("instructions"), f"{prefix}{agent_file.name}", options, result)

        model = ((agent_data or {}).get("aISettings") or {}).get("model") or {}
        if model.get("modelNameHint"):
            hint = str(model["modelNameHint"])
            spec.model.requested = Value[str].declared(hint, f"{prefix}agent.mcs.yml#aISettings.model.modelNameHint")
            if model.get("provider"):
                provider = str(model["provider"])
                spec.model.provider = Value[str].declared(PROVIDERS.get(provider, provider.lower()), f"{prefix}agent.mcs.yml#aISettings.model.provider")
            elif hint.lower().startswith("gpt"):
                spec.model.provider = Value[str].derived("azure-openai", "Copilot Studio serves GPT models through Azure OpenAI")
            elif hint.lower().startswith("claude"):
                spec.model.provider = Value[str].derived("anthropic", "Claude model name")
        else:
            spec.model.requested = Value[str].unknown("Copilot Studio's default model")
        for key in ("conversationStarters", "gptCapabilities", "responseInstructions", "defaultResponseMode"):
            if (agent_data or {}).get(key) not in (None, [], {}):
                extension[key] = agent_data[key]
        ai_settings = {k: v for k, v in ((agent_data or {}).get("aISettings") or {}).items() if k != "model"}
        if ai_settings:
            extension["aISettings"] = ai_settings

        for capability, enabled in ((agent_data or {}).get("gptCapabilities") or {}).items():
            if enabled is True and capability in _CAPABILITIES:
                ref, scope, rule = _CAPABILITIES[capability]
                spec.tools.append(ToolUse(ref=ref, name=capability, scope=Value[Scope].derived(scope, f"{capability} {rule}")))
                entity(EntityKind.TOOL, ref, capability)

        # Actions (tools)
        modes: list[str] = []
        action_extras: dict[str, Any] = {}
        for file in sorted((workspace / "actions").glob("*.mcs.y*ml")) if (workspace / "actions").is_dir() else []:
            data, comment = load(file)
            if not isinstance(data, dict):
                result.warnings.append(f"{prefix}actions/{file.name}: not parsed")
                continue
            use, mode, extra = self._action(data, comment, file.name, schema_name, connectors, f"{prefix}actions/{file.name}")
            if use is None:
                result.warnings.append(f"{prefix}actions/{file.name}: unrecognized action {((data.get('action') or {}).get('kind'))}")
                continue
            if mode:
                modes.append(mode)
            spec.tools.append(use)
            entity(EntityKind.AGENT if use.ref.startswith("agent/") else EntityKind.TOOL, use.ref, use.name)
            action_extras[file.stem.removesuffix(".mcs")] = extra

        # Sub-agents: separate records, called by this agent.
        sub_records = []
        for sub in sorted((workspace / "agents").glob("*/agent.mcs.y*ml")) if (workspace / "agents").is_dir() else []:
            sub_record = self._sub_agent(sub, workspace, prefix, agent_id, options, result)
            if sub_record is not None:
                sub_records.append(sub_record)
                ref = f"agent/{sub_record.agent}"
                spec.tools.append(ToolUse(ref=ref, name=sub_record.spec.display_name.value, description=sub_record.spec.description.value))
                entity(EntityKind.AGENT, ref, sub_record.spec.display_name.value)

        # Skills
        for file in sorted((workspace / "skills").glob("*.mcs.y*ml")) if (workspace / "skills").is_dir() else []:
            data, comment = load(file)
            if isinstance(data, dict) and data.get("kind") == "SkillDefinition":
                ref = f"tool/botframework.skill.{file.name.split('.')[0]}"
                spec.tools.append(ToolUse(ref=ref, name=comment or file.name.split(".")[0]))
                entity(EntityKind.TOOL, ref, comment)

        # HTTP requests made from topics
        topics: dict[str, Any] = {}
        for file in sorted((workspace / "topics").glob("*.mcs.y*ml")) if (workspace / "topics").is_dir() else []:
            data, comment = load(file)
            if not isinstance(data, dict):
                continue
            trigger = (data.get("beginDialog") or {}).get("kind")
            topics[file.name.split(".mcs")[0]] = {"name": comment, "trigger": trigger} if comment else {"trigger": trigger}
            for node in _walk(data):
                if node.get("kind") == "HttpRequestAction" and isinstance(node.get("url"), str):
                    method = str(node.get("method") or "GET").upper()
                    ref = f"tool/http.{method}.{node['url']}"
                    if all(t.ref != ref for t in spec.tools):
                        read = method in _READ_METHODS
                        spec.tools.append(ToolUse(
                            ref=ref, name=node.get("displayName") or f"{method} {node['url']}",
                            scope=Value[Scope].derived(Scope.READ if read else Scope.WRITE, f"HTTP {method} in topic {file.name}"),
                        ))
                        entity(EntityKind.TOOL, ref)
        if topics:
            extension["topics"] = topics

        # Knowledge
        for file in sorted((workspace / "knowledge").rglob("*.mcs.y*ml")) if (workspace / "knowledge").is_dir() else []:
            data, comment = load(file)
            item = self._knowledge(data, comment, file, workspace, agent_id)
            if item is not None:
                spec.knowledge.append(item)
                entity(EntityKind.KNOWLEDGE, item.ref, item.name)

        # Triggers: the agent acts without a user present.
        triggers = []
        for file in sorted((workspace / "trigger").glob("*.mcs.y*ml")) if (workspace / "trigger").is_dir() else []:
            data, comment = load(file)
            source = (data or {}).get("externalTriggerSource") or {}
            reference = source.get("connectionReference")
            triggers.append({
                "name": comment or file.name.split(".mcs")[0],
                **({"connector": connector_name(str(reference), connectors)} if reference else {}),
            })
            if reference:
                modes.append("Maker")
        if triggers:
            extension["triggers"] = triggers

        # Identity, from the connection modes of tools and triggers.
        if modes:
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
            platform=str(marshal.get("platform") or FORMAT),
            source=Source(format=FORMAT, artifact=f"{prefix}{agent_file.name}", detected_by="Copilot Studio agent workspace",
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

    def _action(self, data, comment, filename, schema_name, connectors, where):
        action = data.get("action") or {}
        kind = action.get("kind")
        name = data.get("modelDisplayName") or comment or filename.split(".")[0]
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
        elif kind == "InvokeAIBuilderModelTaskAction":
            use.ref = f"tool/powerplatform.ai_builder.{action.get('aIModelId') or name}"
        else:
            return None, None, extra
        if mode:
            extra["credentials"] = "end user" if mode == "Invoker" else "maker"
        return use, mode, extra

    def _sub_agent(self, path, workspace, prefix, parent_id, options, result):
        data, comment = load(path)
        if not isinstance(data, dict) or data.get("kind") != "AgentDialog":
            return None
        folder = path.parent.name
        spec = AgentSpec()
        name = comment or folder
        spec.display_name = Value[str].declared(name, f"{prefix}agents/{folder}/agent.mcs.yml")
        description = (data.get("beginDialog") or {}).get("description")
        if description:
            spec.description = Value[str].declared(str(description), f"{prefix}agents/{folder}/agent.mcs.yml#beginDialog.description")
        self._instructions(spec, (data.get("settings") or {}).get("instructions"), f"{prefix}agents/{folder}/agent.mcs.yml", options, result)
        spec.model.requested = Value[str].unknown("sub-agents use the parent agent's model")
        spec.extensions[FORMAT] = {"parent": parent_id, "kind": "AgentDialog"}
        return build_record(
            agent=f"{parent_id}/{folder}",
            platform=FORMAT,
            source=Source(format=FORMAT, artifact=f"{prefix}agents/{folder}/{path.name}", detected_by="Copilot Studio sub-agent",
                          commit=options.commit, pipeline_run=options.pipeline_run),
            spec=spec,
        )

    def _knowledge(self, data, comment, file, workspace, agent_id) -> Optional[KnowledgeUse]:
        if file.parent.name == "files":
            name = comment or file.name.split(".mcs")[0]
            return KnowledgeUse(ref=f"knowledge/copilot-studio.{agent_id}.file.{name}", name=name, type="file",
                                scope=Value[Scope].derived(Scope.READ, "uploaded knowledge files are searched"))
        if not isinstance(data, dict):
            return None
        # Either a KnowledgeSourceConfiguration wrapping a source, or the source itself.
        source = data.get("source") if data.get("kind") == "KnowledgeSourceConfiguration" else data
        if not isinstance(source, dict) or not str(source.get("kind", "")).endswith("Source"):
            return None
        kind = str(source.get("kind") or "KnowledgeSource")
        site = source.get("site")
        name = comment or file.name.split(".mcs")[0]
        ref = f"knowledge/{site}" if isinstance(site, str) else f"knowledge/copilot-studio.{agent_id}.{name}"
        return KnowledgeUse(
            ref=ref, name=name, type=_KNOWLEDGE_TYPES.get(kind) or re.sub(r"(?<!^)(?=[A-Z])", "_", kind.removesuffix("Source")).lower(),
            scope=Value[Scope].derived(Scope.READ, f"{kind} is a search source"),
        )
