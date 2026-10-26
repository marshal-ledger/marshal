"""Writer for Salesforce Agentforce Agent Script (``.agent``) files."""

from __future__ import annotations

import json
import re
from typing import Any

from ..model import MARSHAL_NAMESPACE, AgentVersion, IdentityKind
from ..readers.agentforce import FORMAT
from .base import Writer, Written

_INDENT = "    "
_ACTION_SCHEMES = {"flow": "flow", "apex": "apex", "prompt": "prompt", "standard": "standardInvocableAction"}
_DEFAULT_MESSAGES = {
    "welcome": "Hello! How can I help you today?",
    "error": "Sorry, something went wrong. Please try again.",
}


def _quote(value: Any) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (int, float)):
        return str(value)
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _indented(text: str, depth: int) -> list[str]:
    prefix = _INDENT * depth
    return [prefix + line if line.strip() else "" for line in text.splitlines()]


def _mapping(data: dict[str, Any], depth: int) -> list[str]:
    lines = []
    for key, value in data.items():
        if isinstance(value, dict):
            lines.append(f"{_INDENT * depth}{key}:")
            lines += _mapping(value, depth + 1)
        elif isinstance(value, str) and "\n" in value:
            lines.append(f"{_INDENT * depth}{key}: |")
            lines += _indented(value, depth + 1)
        else:
            lines.append(f"{_INDENT * depth}{key}: {_quote(value)}")
    return lines


def _api_name(text: str) -> str:
    name = re.sub(r"[^0-9A-Za-z_]+", "_", text).strip("_") or "Agent"
    name = re.sub(r"_+", "_", name)
    return name if name[0].isalpha() else f"Agent_{name}"


class AgentforceWriter(Writer):
    format = FORMAT
    suffix = ".agent"
    reader_format = FORMAT
    LOSSES = {
        "tools[*]": "Agentforce actions target Flows, Apex classes, prompt templates and standard actions",
        "tools[*].scope": "Agent Script actions don't declare a scope",
        "tools[*].reversible": "Agent Script actions have no reversibility field",
        "knowledge[*]": "Agent Script grounds on an Agentforce data library only",
        "knowledge[*].scope": "a data library is always searched read-only",
        "model.*": "the Agentforce reasoning engine chooses the model",
        "permissions.grants[*]": "permissions come from the agent user's permission sets, outside the agent definition",
        "permissions.access[*]": "who can use the agent is set by deployment channels and permission sets",
        "permissions.identity": "Agent Script names a dedicated agent user, or runs employee agents as the signed-in user",
    }

    def write(self, record: AgentVersion) -> Written:
        spec = record.spec
        notes: dict[str, str] = {}
        warnings: list[str] = []
        ext: dict[str, Any] = dict(spec.extensions.get(FORMAT, {}))
        for platform in spec.extensions:
            if platform != FORMAT:
                notes[f"extensions.{platform}.*"] = f"{platform}-specific; no Agentforce equivalent"
        lines: list[str] = []

        config = dict(ext.get("config") or {})
        developer_name = config.pop("developer_name", None) or _api_name(record.agent)
        marshal: dict[str, str] = {}
        if developer_name != record.agent:
            marshal["agent"] = record.agent
        if record.platform != FORMAT:
            marshal["platform"] = record.platform
        if marshal:
            lines.append(f"# {MARSHAL_NAMESPACE}: {json.dumps(marshal)}")
            lines.append("")

        # system
        lines.append("system:")
        if spec.instructions.text:
            if spec.instructions.text != spec.instructions.text.strip():
                notes["instructions"] = "Agent Script block text drops leading and trailing whitespace"
            lines.append(f"{_INDENT}instructions: |")
            lines += _indented(spec.instructions.text, 2)
        else:
            lines.append(f'{_INDENT}instructions: "You are a helpful assistant."')
            notes["instructions"] = (
                "instruction text is not available (hash-only source)" if spec.instructions.hash
                else "Agent Script requires system instructions; a generic instruction was written"
            )
        messages = ext.get("messages")
        if not messages:
            messages = dict(_DEFAULT_MESSAGES)
            notes["extensions.agentforce.messages"] = "Agent Script requires welcome and error messages; generic ones were written"
        lines.append(f"{_INDENT}messages:")
        lines += _mapping(messages, 2)
        lines.append("")

        # access
        access = dict(ext.get("access") or {})
        principal = spec.permissions.principal.value if spec.permissions.principal.is_known else None
        if principal and spec.permissions.identity.value is IdentityKind.SERVICE_PRINCIPAL:
            access = {"default_agent_user": principal, **access}
        elif principal:
            notes["permissions.principal"] = "an Agentforce agent user is a Salesforce user, not this principal"
        if access:
            lines.append("access:")
            lines += _mapping(access, 1)
            lines.append("")

        # config
        lines.append("config:")
        config_out: dict[str, Any] = {"developer_name": developer_name}
        if spec.display_name.is_known:
            config_out["agent_label"] = spec.display_name.value
        else:
            config_out["agent_label"] = record.agent
            notes["display_name"] = "Agent Script requires an agent label; the agent id was used"
        if spec.description.is_known:
            config_out["description"] = spec.description.value
        config_out.update(config)
        if "agent_type" not in config_out and not ext.get("config"):
            config_out["agent_type"] = (
                "AgentforceEmployeeAgent" if spec.permissions.identity.value is IdentityKind.ON_BEHALF_OF_USER
                else "AgentforceServiceAgent"
            )
            notes["extensions.agentforce.config"] = "the agent type was chosen from the agent's identity"
        lines += _mapping(config_out, 1)
        lines.append("")

        if ext.get("knowledge"):
            lines.append("knowledge:")
            lines += _mapping(ext["knowledge"], 1)
            lines.append("")
        else:
            libraries = [k for k in spec.knowledge if k.ref.startswith("knowledge/salesforce.data_library.")]
            if libraries:
                lines.append("knowledge:")
                lines += _mapping({"rag_feature_config_id": libraries[0].ref.rsplit(".", 1)[-1]}, 1)
                lines.append("")
                notes["extensions.agentforce.knowledge"] = "the data library was written from the knowledge ref"
        for name in ("variables", "language"):
            if ext.get(name):
                lines.append(f"{name}:")
                lines += _indented(ext[name], 1)
                lines.append("")
        if not ext.get("language") and not ext.get("subagents"):
            lines += ["language:", f'{_INDENT}default_locale: "en_US"', ""]
            notes["extensions.agentforce.language"] = "Agent Script requires a default locale; en_US was written"
        for name, text in (ext.get("blocks") or {}).items():
            lines.append(f"{name}:")
            lines += _indented(text, 1)
            lines.append("")

        subagents = ext.get("subagents")
        if subagents:
            for name, block in subagents.items():
                lines.append(f"{block['kind']} {name}:")
                lines += _indented(block["source"], 1)
                lines.append("")
        else:
            lines += self._entry_subagent(record, notes)
            notes["extensions.agentforce.subagents"] = "one entry subagent was generated to hold the agent's actions"
        return Written(document={"source": "\n".join(lines).rstrip() + "\n"}, notes=notes, warnings=warnings,
                       files={f"{developer_name}{self.suffix}": "\n".join(lines).rstrip() + "\n"})

    def dump(self, document: dict[str, Any]) -> str:
        return str(document["source"])

    def _entry_subagent(self, record: AgentVersion, notes: dict[str, str]) -> list[str]:
        spec = record.spec
        actions: list[tuple[str, Any, str]] = []
        escalate = False
        used: set[str] = set()
        for use in spec.tools:
            if use.ref == "tool/salesforce.escalate":
                escalate = True
                continue
            identifier = use.ref.removeprefix("tool/salesforce.")
            scheme, _, target = identifier.partition(".")
            if not use.ref.startswith("tool/salesforce.") or scheme not in _ACTION_SCHEMES or not target:
                continue
            name = _api_name(use.name or target)
            while name in used:
                name += "_"
            used.add(name)
            if use.name and name != use.name:
                notes[f"tools[{use.ref}].name"] = "Agent Script action names are API names"
            actions.append((name, use, f"{_ACTION_SCHEMES[scheme]}://{target}"))

        description = str(spec.description.value or "Handles the user's requests")
        lines = ["start_agent main:", f'{_INDENT}label: "Main"', f"{_INDENT}description: {_quote(description)}",
                 f"{_INDENT}reasoning:", f"{_INDENT * 2}instructions: ->",
                 f"{_INDENT * 3}| Help the user with their request, using the available actions when they apply."]
        if actions or escalate:
            lines.append(f"{_INDENT * 2}actions:")
            for name, _, _ in actions:
                lines.append(f"{_INDENT * 3}{name}: @actions.{name}")
            if escalate:
                lines.append(f"{_INDENT * 3}escalate_to_human: @utils.escalate")
        if actions:
            lines.append(f"{_INDENT}actions:")
            for name, use, target in actions:
                lines.append(f"{_INDENT * 2}{name}:")
                if use.description:
                    lines.append(f"{_INDENT * 3}description: {_quote(use.description)}")
                lines += self._parameters("inputs", use.input_schema, 3, inputs=True, notes=notes,
                                          path=f"tools[{use.ref}].input_schema")
                lines += self._parameters("outputs", use.output_schema, 3, inputs=False, notes=notes,
                                          path=f"tools[{use.ref}].output_schema")
                lines.append(f"{_INDENT * 3}target: {_quote(target)}")
                if use.requires_confirmation.is_known:
                    lines.append(f"{_INDENT * 3}require_user_confirmation: {_quote(bool(use.requires_confirmation.value))}")
        return lines

    def _parameters(self, label: str, schema: Any, depth: int, inputs: bool, notes: dict[str, str] | None = None,
                    path: str = "") -> list[str]:
        if not isinstance(schema, dict) or not schema.get("properties"):
            return []
        if notes is not None and any(not isinstance(p, dict) or "type" not in p or set(p) - {"type", "description", "items"}
                                     or (p.get("type") == "array" and "items" not in p)
                                     for p in schema["properties"].values()):
            notes[path] = "Agent Script parameters keep a type and a description; untyped parameters are written as string"
        if notes is not None and any(not re.fullmatch(r"[A-Za-z_]\w*", n) for n in schema["properties"]):
            notes[path] = "Agent Script parameter names must be identifiers; other names don't read back as written"
        required = set(schema.get("required") or [])
        lines = [f"{_INDENT * depth}{label}:"]
        for name, prop in schema["properties"].items():
            kind = prop.get("type", "string") if isinstance(prop, dict) else "string"
            if kind == "array":
                kind = f"list[{(prop.get('items') or {}).get('type', 'string')}]"
            lines.append(f"{_INDENT * (depth + 1)}{name}: {kind}")
            if isinstance(prop, dict) and prop.get("description"):
                lines.append(f"{_INDENT * (depth + 2)}description: {_quote(prop['description'])}")
            if inputs:
                lines.append(f"{_INDENT * (depth + 2)}is_required: {_quote(name in required)}")
        return lines
