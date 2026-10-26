"""Writer for Google ADK Agent Config (``root_agent.yaml``)."""

from __future__ import annotations

import json
import re
from typing import Any

import yaml

from ..model import MARSHAL_NAMESPACE, AgentVersion
from ..readers.adkconfig import BUILTINS, FORMAT, ROOT_FILE
from .base import Writer, Written

_BUILTIN_REFS = {ref: name for name, (ref, _) in BUILTINS.items()}
_PARAMETERS = {"temperature": "temperature", "top_p": "top_p", "max_tokens": "max_output_tokens"}


def _identifier(text: str) -> str:
    name = re.sub(r"\W+", "_", text).strip("_") or "agent"
    return name if name[0].isalpha() or name[0] == "_" else f"agent_{name}"


class AdkConfigWriter(Writer):
    format = FORMAT
    suffix = ".adk"
    reader_format = FORMAT
    multi_file = True
    LOSSES = {
        "tools[*]": "ADK Agent Config tools are built-ins, Python functions and MCP toolsets",
        "tools[agent/*]": "sub-agents are separate config files; convert them on their own",
        "tools[*].name": "ADK names a Python function tool by its import path",
        "tools[*].description": "a function tool's description comes from its docstring, in code",
        "tools[*].input_schema": "a function tool's schema comes from its signature, in code",
        "tools[*].output_schema": "a function tool's schema comes from its signature, in code",
        "tools[*].scope": "ADK tools don't declare a scope",
        "tools[*].reversible": "ADK tools have no reversibility field",
        "tools[*].requires_confirmation": "confirmation is implemented in code, for example with LongRunningFunctionTool",
        "knowledge[*]": "ADK Agent Config grounds through tools, not knowledge sources",
        "permissions.*": "ADK Agent Config doesn't describe identity or permissions",
        "model.provider": "ADK Agent Config supports Gemini models, so the provider is implied",
        "model.resolved": "the config names the requested model only",
    }

    def write(self, record: AgentVersion) -> Written:
        spec = record.spec
        notes: dict[str, str] = {}
        warnings: list[str] = []
        ext: dict[str, Any] = dict(spec.extensions.get(FORMAT, {}))
        for platform in spec.extensions:
            if platform != FORMAT:
                notes[f"extensions.{platform}.*"] = f"{platform}-specific; no ADK Agent Config equivalent"

        name = str(spec.display_name.value or record.agent.rsplit("/", 1)[-1])
        identifier = _identifier(name)
        if not spec.display_name.is_known:
            notes["display_name"] = "ADK requires an agent name; one was derived from the agent id"
        elif identifier != name:
            notes["display_name"] = "ADK agent names must be identifiers"
        doc: dict[str, Any] = {"name": identifier}
        if spec.model.requested.is_known:
            doc["model"] = spec.model.requested.value
            if not str(spec.model.requested.value).startswith("gemini"):
                warnings.append("ADK Agent Config currently supports Gemini models only")
        if spec.description.is_known:
            doc["description"] = spec.description.value
        if spec.instructions.text:
            doc["instruction"] = spec.instructions.text
        elif spec.instructions.hash:
            notes["instructions"] = "instruction text is not available (hash-only source)"

        tools: list[dict[str, Any]] = []
        extras = {t["name"]: t for t in ext.pop("tools", []) if isinstance(t, dict) and "name" in t}
        for use in spec.tools:
            if use.ref in _BUILTIN_REFS:
                tools.append({"name": _BUILTIN_REFS[use.ref]})
            elif use.ref.startswith("tool/python.") and ":" in use.ref:
                module, function = use.ref.removeprefix("tool/python.").split(":", 1)
                module = module.split("/", 1)[-1]  # drop the project scope; ADK imports are project-relative
                path = f"{module}.{function}"
                wrapped = next((t for t in extras.values() if (t.get("args") or {}).get("func") == path), None)
                tools.append(wrapped if wrapped else {"name": path})
            elif use.ref.startswith("tool/mcp."):
                server = use.ref.removeprefix("tool/mcp.")
                existing = extras.get("McpToolset")
                if existing:
                    tools.append(existing)
                    continue
                notes["extensions.adk-config.tools"] = "MCP toolset connections were written from the tool refs"
                if server.startswith(("http://", "https://")):
                    tools.append({"name": "McpToolset", "args": {"streamable_http_connection_params": {"url": server}}})
                else:
                    command, *args = server.split()
                    tools.append({"name": "McpToolset", "args": {"stdio_connection_params": {"server_params": {"command": command, "args": args}}}})
        if tools:
            doc["tools"] = tools

        config = dict(ext.pop("generate_content_config", {}) or {})
        for key, value in spec.model.parameters.items():
            if key in _PARAMETERS:
                config[_PARAMETERS[key]] = value
        if config:
            doc["generate_content_config"] = config
        for key, value in ext.items():
            doc[key] = value

        marshal = {"agent": record.agent}
        if record.platform != "google-adk":
            marshal["platform"] = record.platform
        text = f"# {MARSHAL_NAMESPACE}: {json.dumps(marshal)}\n" + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100)
        return Written(document=doc, notes=notes, warnings=warnings, files={ROOT_FILE: text})
