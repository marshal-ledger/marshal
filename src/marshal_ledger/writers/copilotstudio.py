"""Writer for Microsoft Copilot Studio agent workspaces (``.mcs.yml`` files).

Produces the files the Copilot Studio extension for Visual Studio Code applies
to an environment: ``agent.mcs.yml``, ``settings.mcs.yml``, one file per action
and knowledge source, and ``connectionreferences.mcs.yml`` for the connectors
the actions use. Copilot Studio tools are connectors, flows, MCP connectors and
other agents; tools of other kinds are reported as dropped.
"""

from __future__ import annotations

import json
import re
from typing import Any

import yaml

from ..model import MARSHAL_NAMESPACE, AgentVersion, IdentityKind
from ..readers.copilotstudio import FORMAT, PROVIDERS
from .base import Writer, Written

_POLICIES = {
    "anyone in the tenant": "Any",
    "anyone in any tenant": "AnyMultiTenant",
    "users the agent is shared with": "ChatbotReaders",
    "members of the configured security groups": "GroupMembership",
}
_CAPABILITY_TOOLS = {"tool/copilot-studio.web_search": "webBrowsing", "tool/copilot-studio.code_interpreter": "codeInterpreter"}


def _file_name(text: str) -> str:
    name = re.sub(r"[^0-9A-Za-z]+", "", text.title()) or "Component"
    return name[:60]


class _Dumper(yaml.SafeDumper):
    """Writes multi-line text, such as instructions, as a literal block that reads as written."""


_Dumper.add_representer(
    str, lambda dumper, value: dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|" if "\n" in value else None)
)


def _dump(data: Any, **options: Any) -> str:
    return yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, **options)


def _yaml(data: dict[str, Any], name: str | None = None, description: str | None = None) -> str:
    header = ""
    if name:
        header += f"# Name: {name}\n"
    if description:
        header += f"# {description.splitlines()[0]}\n"
    return header + _dump(data, width=100)


class CopilotStudioWriter(Writer):
    format = FORMAT
    suffix = ".copilot-studio"
    reader_format = FORMAT
    multi_file = True
    LOSSES = {
        "tools[*]": "Copilot Studio tools are connectors, flows, MCP connectors and agents; this tool needs a custom connector",
        "tools[*].scope": "Copilot Studio actions don't declare a scope",
        "tools[*].reversible": "Copilot Studio actions have no reversibility field",
        "tools[*].output_schema": "action outputs are defined by the connector",
        "knowledge[*]": "Copilot Studio knowledge sources are websites, SharePoint sites and uploaded files",
        "knowledge[*].description": "knowledge source files carry a name, not a description",
        "permissions.grants[*]": "permissions come from each connection's credentials, not from grants on the agent",
        "permissions.principal": "connections authenticate as the maker or the end user, not as a named principal",
        "permissions.identity": "Copilot Studio derives identity from each connection's credential mode",
        "permissions.access[*]": "Copilot Studio controls access with one policy for the whole agent",
        "model.parameters.*": "Copilot Studio doesn't expose generation parameters",
        "model.provider": "the provider is implied by the model",
        "model.resolved": "Copilot Studio names the requested model only",
        "description": "the agent's description is shown in Copilot Studio, not stored in the agent definition",
    }

    def write(self, record: AgentVersion) -> Written:
        spec = record.spec
        notes: dict[str, str] = {}
        warnings: list[str] = []
        ext: dict[str, Any] = dict(spec.extensions.get(FORMAT, {}))
        for platform in spec.extensions:
            if platform != FORMAT:
                notes[f"extensions.{platform}.*"] = f"{platform}-specific; no Copilot Studio equivalent"
        files: dict[str, str] = {}
        name = str(spec.display_name.value or record.agent.rsplit("/", 1)[-1])
        if not spec.display_name.is_known:
            notes["display_name"] = "Copilot Studio requires a display name; one was derived from the agent id"
        schema_name = re.sub(r"[^0-9A-Za-z_]+", "_", record.agent).strip("_") or "agent"
        marshal: dict[str, str] = {}
        if schema_name != record.agent:
            marshal["agent"] = record.agent
        if record.platform != FORMAT:
            marshal["platform"] = record.platform

        agent: dict[str, Any] = {"kind": "GptComponentMetadata"}
        if spec.instructions.text:
            agent["instructions"] = spec.instructions.text
        elif spec.instructions.hash:
            notes["instructions"] = "instruction text is not available (hash-only source)"
        for key in ("conversationStarters", "responseInstructions", "defaultResponseMode"):
            if key in ext:
                agent[key] = ext[key]
        capabilities = dict(ext.get("gptCapabilities") or {})
        for use in spec.tools:
            if use.ref in _CAPABILITY_TOOLS:
                capabilities[_CAPABILITY_TOOLS[use.ref]] = True
        if capabilities:
            agent["gptCapabilities"] = capabilities
        ai_settings = dict(ext.get("aISettings") or {})
        if spec.model.requested.is_known:
            ai_settings["model"] = {"modelNameHint": spec.model.requested.value}
            provider = {v: k for k, v in PROVIDERS.items()}.get(str(spec.model.provider.value))
            if spec.model.provider.is_known and provider:
                ai_settings["model"]["provider"] = provider
            elif spec.model.provider.is_known:
                notes["model.provider"] = f"Copilot Studio model providers are {', '.join(PROVIDERS)}"
        if ai_settings:
            agent["aISettings"] = ai_settings
        files["agent.mcs.yml"] = _yaml(agent)

        settings: dict[str, Any] = {"displayName": name, "schemaName": schema_name}
        if not ext.get("settings"):
            notes["extensions.copilot-studio.settings"] = (
                "Copilot Studio requires settings; the agent was limited to users it is shared with, with integrated authentication"
            )
        for key, value in (ext.get("settings") or {}).items():
            if key != "schemaName":
                settings[key] = value
        if "accessControlPolicy" not in settings:
            policies = [_POLICIES[a.principal] for a in spec.permissions.access if a.principal in _POLICIES]
            settings["accessControlPolicy"] = policies[0] if policies else "ChatbotReaders"
            if not policies:
                notes["permissions.access[users the agent is shared with chat]"] = (
                    "Copilot Studio requires an access policy; the agent was limited to users it is shared with"
                )
                notes.setdefault("extensions.copilot-studio.settings", "the source sets no access policy; one was added")
        if "authenticationMode" not in settings:
            settings["authenticationMode"] = "Integrated"
            notes.setdefault("extensions.copilot-studio.settings", "the source sets no authentication mode; integrated was added")
        header = f"# {MARSHAL_NAMESPACE}: {json.dumps(marshal)}\n" if marshal else ""
        files["settings.mcs.yml"] = header + _dump(settings)

        default_mode = "Invoker" if spec.permissions.identity.value is IdentityKind.ON_BEHALF_OF_USER else "Maker"
        action_extras = ext.get("actions") or {}
        extras_by_name = {}
        for key, extra in action_extras.items():
            extras_by_name[key] = extra
        connectors: dict[str, str] = {}
        used: set[str] = set()
        for use in spec.tools:
            if use.ref in _CAPABILITY_TOOLS:
                continue
            action = self._action(use, schema_name, connectors, notes)
            if action is None:
                continue
            extra = extras_by_name.get(_file_name(use.name or "")) or (ext.get("topic_calls") or {}).get(use.ref) or next(
                (e for e in action_extras.values() if e.get("connector") and use.ref.endswith(str(e.get("connector")))), {}
            )
            if use.ref in (ext.get("topic_calls") or {}):
                notes[f"tools[{use.ref}].name"] = (
                    "the source calls this from a topic; it is written as an action, named after the operation"
                )
            if "connectionReference" in action:
                mode = extra.get("connection_mode")
                if not mode:
                    mode = default_mode
                    notes.setdefault("extensions.copilot-studio.actions", (
                        "the source states no connection mode, so each action's was set from the agent's identity: "
                        "Invoker for an agent that acts on behalf of users, otherwise Maker"
                    ))
                action["connectionProperties"] = {"mode": mode}
            dialog: dict[str, Any] = {"kind": "TaskDialog"}
            inputs = self._inputs(use.input_schema)
            if inputs:
                dialog["inputs"] = inputs
            dialog["modelDisplayName"] = use.name or use.ref.split("/", 1)[-1]
            if use.description:
                dialog["modelDescription"] = use.description
            if use.requires_confirmation.is_known and use.requires_confirmation.value:
                dialog["confirmation"] = {"activity": f"Do you want me to run {dialog['modelDisplayName']}?"}
            dialog["action"] = action
            filename = _file_name(dialog["modelDisplayName"])
            while filename in used:
                filename += "2"
            used.add(filename)
            files[f"actions/{filename}.mcs.yml"] = _yaml(dialog, dialog["modelDisplayName"], use.description)

        if connectors:
            files["connectionreferences.mcs.yml"] = yaml.safe_dump({"connectionReferences": [
                {"connectionReferenceLogicalName": logical, "connectorId": f"/providers/Microsoft.PowerApps/apis/{connector}"}
                for logical, connector in sorted(connectors.items())
            ]}, sort_keys=False)

        for item in spec.knowledge:
            identifier = item.ref.split("/", 1)[-1]
            title = item.name or identifier
            if identifier.startswith(("https://", "http://")):
                kind = "SharePointSearchSource" if ".sharepoint.com" in identifier else "PublicSiteSearchSource"
                files[f"knowledge/{_file_name(title)}.mcs.yml"] = _yaml({"kind": kind, "site": identifier}, title)
                expected = "sharepoint" if kind == "SharePointSearchSource" else "public_website"
                if item.type and item.type != expected:
                    notes[f"knowledge[{item.ref}].type"] = f"Copilot Studio searches a URL as a {expected.replace('_', ' ')} source"
            elif identifier.startswith(f"copilot-studio.{record.agent}.file."):
                files[f"knowledge/files/{_file_name(title)}.mcs.yml"] = f"# Name: {title}\n"

        if ext.get("kind") == "AgentDialog":
            for key in ("parent", "kind"):
                notes[f"extensions.copilot-studio.{key}"] = "a sub-agent converted on its own is written as a standalone agent"
        triggers = ext.get("triggers")
        if triggers:
            notes["extensions.copilot-studio.triggers"] = "triggers are configured with their own connections, which aren't in the record"
        for key in ("topics", "actions"):
            if key in ext:
                notes[f"extensions.copilot-studio.{key}"] = f"{key} are recorded by name, not by content, and are not re-created"
        if "topic_calls" in ext:
            notes["extensions.copilot-studio.topic_calls"] = "topics are not re-created, so their calls are written as actions"
            notes.setdefault("extensions.copilot-studio.actions", "the source's topic calls are written as actions")
        if not spec.model.requested.is_known:
            warnings.append("the source names no model; Copilot Studio will use its default")
        return Written(document=agent, notes=notes, files=files, warnings=warnings)

    def _action(self, use: Any, schema_name: str, connectors: dict[str, str], notes: dict[str, str]) -> dict[str, Any] | None:
        identifier = use.ref.split("/", 1)[-1]
        if use.ref.startswith("agent/"):
            target = identifier
            if target.startswith(f"{schema_name}/") or "/" in target:
                notes[f"tools[{use.ref}]"] = "sub-agents and agents from other formats are separate records; convert them on their own"
                return None
            return {"kind": "InvokeConnectedAgentTaskAction", "botSchemaName": target}
        if identifier.startswith("powerplatform.ai_plugin."):
            plugin = identifier.removeprefix("powerplatform.ai_plugin.")
            return {
                "kind": "InvokeAIPluginTaskAction",
                "entityKey": f"aiplugin.name={plugin},operationid={plugin}",
                "primaryConnectionReference": {"kind": "InvokerConnectionReference"},
            }
        if identifier.startswith("powerplatform.flow."):
            return {"kind": "InvokeFlowTaskAction", "flowId": identifier.removeprefix("powerplatform.flow.")}
        if identifier.startswith("mcp.powerplatform."):
            connector = identifier.removeprefix("mcp.powerplatform.")
            logical = f"{schema_name}.{connector}"
            connectors[logical] = connector
            return {
                "kind": "InvokeExternalAgentTaskAction",
                "connectionReference": logical,
                "operationDetails": {"kind": "ModelContextProtocolMetadata", "operationId": use.name or connector},
            }
        if identifier.startswith("powerplatform."):
            connector, _, operation = identifier.removeprefix("powerplatform.").partition(".")
            if not operation:
                notes[f"tools[{use.ref}]"] = "the source names the connector but not the operation, which an action requires"
                return None
            logical = f"{schema_name}.{connector}"
            connectors[logical] = connector
            return {"kind": "InvokeConnectorTaskAction", "connectionReference": logical, "operationId": operation}
        return None

    def _inputs(self, schema: Any) -> list[dict[str, Any]]:
        if not isinstance(schema, dict):
            return []
        required = set(schema.get("required") or [])
        inputs = []
        for name, prop in (schema.get("properties") or {}).items():
            item: dict[str, Any] = {"kind": "AutomaticTaskInput", "propertyName": name}
            if isinstance(prop, dict) and prop.get("description"):
                item["description"] = prop["description"]
            if name not in required:
                item["defaultValue"] = "=Blank()"
            inputs.append(item)
        return inputs
