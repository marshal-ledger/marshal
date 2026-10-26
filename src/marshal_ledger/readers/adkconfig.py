"""Reader for Google ADK Agent Config (``root_agent.yaml``).

ADK Agent Config declares an agent in YAML: name, model, description,
instruction, tools and sub-agents, which may live in other files
(``config_path``). The reader starts from each ``root_agent.yaml`` and follows
``config_path`` references. Workflow agents (``SequentialAgent``,
``ParallelAgent``, ``LoopAgent``) are reported as skipped; their sub-agents are
recorded.

Tool refs match the Python reader's, so a function or built-in used by an agent
in YAML and an agent in code is the same entity. MCP server environment values
are never copied.

Mapping rules are documented in ``docs/formats/adk-config.md``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

import yaml

from ..hashing import sha256_text
from ..model import (
    MARSHAL_NAMESPACE,
    AgentSpec,
    Entity,
    EntityKind,
    Instructions,
    Provenance,
    Scope,
    Source,
    ToolUse,
    Value,
)
from ..redact import redact
from .base import ReadOptions, ReadResult, Reader, Skipped, build_record, jsonable

FORMAT = "adk-config"
ROOT_FILE = "root_agent.yaml"
WORKFLOW_AGENTS = {"SequentialAgent", "ParallelAgent", "LoopAgent"}
BUILTINS = {
    "google_search": ("tool/google.search", Scope.READ),
    "url_context": ("tool/google.url_context", Scope.READ),
    "enterprise_web_search": ("tool/google.enterprise_web_search", Scope.READ),
    "google_maps_grounding": ("tool/google.maps_grounding", Scope.READ),
    "load_web_page": ("tool/google.load_web_page", Scope.READ),
    "VertexAiSearchTool": ("tool/google.vertex_ai_search", Scope.READ),
    "built_in_code_execution": ("tool/google.code_execution", Scope.EXECUTE),
    "exit_loop": ("tool/google.exit_loop", Scope.EXECUTE),
    "get_user_choice": ("tool/google.get_user_choice", Scope.READ),
    "load_artifacts": ("tool/google.load_artifacts", Scope.READ),
    "load_memory": ("tool/google.load_memory", Scope.READ),
    "preload_memory": ("tool/google.preload_memory", Scope.READ),
}
_PARAMETERS = {"temperature": "temperature", "top_p": "top_p", "max_output_tokens": "max_tokens"}
_MARSHAL_COMMENT = re.compile(r"^#\s*" + re.escape(MARSHAL_NAMESPACE) + r":\s*(\{.*\})\s*$", re.MULTILINE)


def marshal_comment(path: Path) -> dict[str, Any]:
    """Identity a Marshal conversion recorded in a comment, so a converted agent reads back as itself."""
    try:
        match = _MARSHAL_COMMENT.search(path.read_text(encoding="utf-8"))
        value = json.loads(match.group(1)) if match else {}
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def function_ref(path: str, project: str) -> str:
    """The ref the Python reader gives the same function when it reads the app from ``project``."""
    module, _, function = path.rpartition(".")
    if not module:
        return f"tool/python.{project}/{function}"
    return f"tool/python.{project}/{module}:{function}"


def _load(path: Path) -> Any:
    try:
        return jsonable(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None


def _mcp_server(args: dict[str, Any]) -> Optional[str]:
    for key in ("streamable_http_connection_params", "sse_connection_params", "connection_params"):
        params = args.get(key)
        if isinstance(params, dict) and isinstance(params.get("url"), str):
            return params["url"]
    stdio = args.get("stdio_connection_params") or args.get("stdio_server_params") or {}
    server = stdio.get("server_params", stdio) if isinstance(stdio, dict) else {}
    if isinstance(server, dict) and isinstance(server.get("command"), str):
        return " ".join([server["command"], *[str(a) for a in server.get("args") or []]])
    return None


def _sanitize(value: Any) -> Any:
    """Tool arguments without environment values or headers."""
    if isinstance(value, dict):
        return {k: ("<redacted>" if k in ("env", "headers") else _sanitize(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    return value


class AdkConfigReader(Reader):
    format = FORMAT
    platform = "google-adk"

    @classmethod
    def detect(cls, path: Path) -> bool:
        return any(True for _ in cls._roots(path))

    @staticmethod
    def _roots(path: Path):
        if path.is_file():
            if path.name == ROOT_FILE:
                yield path
            return
        if path.is_dir():
            for file in sorted(path.rglob(ROOT_FILE)):
                if not any(p in (".git", "node_modules", ".venv", "venv") for p in file.parts):
                    yield file

    def read(self, path: Path, options: ReadOptions | None = None) -> ReadResult:
        options = options or ReadOptions()
        result = ReadResult()
        root = path if path.is_dir() else path.parent
        entities: dict[str, Entity] = {}
        for file in self._roots(path):
            self._read_tree(file, root, options, result, entities, seen=set())
        result.entities = sorted(entities.values(), key=lambda e: e.ref)
        return result

    def _agent_id(self, file: Path, data: dict[str, Any]) -> str:
        return str(marshal_comment(file).get("agent") or f"{file.parent.name}/{data.get('name') or file.stem}")

    def _read_tree(self, file: Path, root: Path, options, result, entities, seen: set[Path]) -> Optional[str]:
        """Read the agent in ``file`` and its sub-agents. Returns its agent id."""
        file = file.resolve()
        if file in seen:
            return None
        seen.add(file)
        data = _load(file)
        artifact = file.relative_to(root.resolve()).as_posix() if file.is_relative_to(root.resolve()) else file.name
        if not isinstance(data, dict) or not data.get("name"):
            result.warnings.append(f"{artifact}: not an ADK agent config")
            return None
        agent_id = self._agent_id(file, data)
        sub_ids = []
        for sub in data.get("sub_agents") or []:
            if isinstance(sub, dict) and isinstance(sub.get("config_path"), str):
                sub_file = file.parent / sub["config_path"]
                if sub_file.is_file():
                    sub_id = self._read_tree(sub_file, root, options, result, entities, seen)
                    if sub_id:
                        sub_ids.append(sub_id)
                else:
                    result.warnings.append(f"{artifact}: sub-agent {sub['config_path']} not found")
        agent_class = str(data.get("agent_class") or "LlmAgent")
        if agent_class in WORKFLOW_AGENTS:
            result.skipped.append(Skipped(artifact, f"{agent_class} {data['name']}",
                                          "ADK workflow agent; its sub-agents are recorded"))
            return None
        if agent_class != "LlmAgent":
            result.skipped.append(Skipped(artifact, f"{agent_class} {data['name']}", f"{agent_class} is not read"))
            return None

        def entity(kind: EntityKind, ref: str, display: Optional[str] = None) -> None:
            entities.setdefault(ref, Entity(ref=ref, kind=kind, platform="google-adk", identifier=ref.split("/", 1)[1], display_name=display))

        spec = AgentSpec()
        extension: dict[str, Any] = {}
        spec.display_name = Value[str].declared(str(data["name"]), f"{artifact}#name")
        if data.get("description"):
            spec.description = Value[str].declared(redact(str(data["description"]))[0], f"{artifact}#description")
        model = data.get("model")
        if isinstance(model, str):
            spec.model.requested = Value[str].declared(model, f"{artifact}#model")
            if model.startswith("gemini"):
                spec.model.provider = Value[str].derived("google", "Gemini model name")
        else:
            spec.model.requested = Value[str].unknown("ADK's default model")
        config = data.get("generate_content_config") or {}
        spec.model.parameters = {_PARAMETERS[k]: v for k, v in config.items() if k in _PARAMETERS and isinstance(v, (int, float))}
        rest = {k: v for k, v in config.items() if k not in _PARAMETERS}
        if rest:
            extension["generate_content_config"] = rest
        instruction = data.get("instruction")
        if isinstance(instruction, str) and instruction.strip():
            text, count = redact(instruction)
            if count:
                result.warnings.append(f"{artifact}: instruction: redacted {count} credential-like value(s)")
            spec.instructions = Instructions(hash=sha256_text(text), text=None if options.hash_only else text,
                                             provenance=Provenance.DECLARED, basis=f"{artifact}#instruction")

        tool_extras = []
        for tool in data.get("tools") or []:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                continue
            name, args = tool["name"], tool.get("args") or {}
            use: Optional[ToolUse] = None
            if name in BUILTINS:
                ref, scope = BUILTINS[name]
                use = ToolUse(ref=ref, name=name, scope=Value[Scope].derived(scope, f"{name} built-in tool"))
            elif name == "McpToolset":
                server = _mcp_server(args)
                if server:
                    use = ToolUse(ref=f"tool/mcp.{server}", name=name)
                else:
                    result.warnings.append(f"{artifact}: McpToolset connection not recognized")
            elif name == "AgentTool" and isinstance((args.get("agent") or {}).get("config_path"), str):
                sub_file = file.parent / args["agent"]["config_path"]
                sub_id = self._read_tree(sub_file, root, options, result, entities, seen) if sub_file.is_file() else None
                if sub_id:
                    use = ToolUse(ref=f"agent/{sub_id}", name=name)
            elif name in ("LongRunningFunctionTool", "FunctionTool") and isinstance(args.get("func"), str):
                use = ToolUse(ref=function_ref(args["func"], file.parent.parent.name), name=args["func"].rsplit(".", 1)[-1])
            elif "." in name:
                use = ToolUse(ref=function_ref(name, file.parent.parent.name), name=name.rsplit(".", 1)[-1])
            if use is None:
                tool_extras.append(_sanitize(tool))
                continue
            if args and name not in BUILTINS:
                tool_extras.append(_sanitize(tool))
            if all(t.ref != use.ref for t in spec.tools):
                spec.tools.append(use)
                entity(EntityKind.AGENT if use.ref.startswith("agent/") else EntityKind.TOOL, use.ref, use.name)
        if tool_extras:
            extension["tools"] = tool_extras

        for sub_id in sub_ids:
            ref = f"agent/{sub_id}"
            if all(t.ref != ref for t in spec.tools):
                spec.tools.append(ToolUse(ref=ref, name=sub_id.rsplit("/", 1)[-1]))
                entity(EntityKind.AGENT, ref)
        for key, value in data.items():
            if key not in ("name", "model", "description", "instruction", "tools", "sub_agents", "agent_class",
                           "generate_content_config"):
                extension[key] = value
        spec.extensions[FORMAT] = extension
        marshal = marshal_comment(file)
        result.records.append(build_record(
            agent=agent_id,
            platform=str(marshal.get("platform") or "google-adk"),
            source=Source(format=FORMAT, artifact=artifact, detected_by="ADK Agent Config",
                          commit=options.commit, pipeline_run=options.pipeline_run),
            spec=spec,
        ))
        return agent_id
