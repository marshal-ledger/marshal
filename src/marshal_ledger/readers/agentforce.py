"""Reader for Salesforce Agentforce Agent Script (``.agent``) files.

Agent Script is the source format of the new Agentforce Builder, kept in an
``AiAuthoringBundle`` (``aiAuthoringBundles/<Name>/<Name>.agent``). It is an
indentation-based language with blocks for the system prompt, the agent user,
configuration, variables, language, knowledge, and subagents (topics) with
reasoning instructions and actions.

The reader maps agent-level facts into the ledger model: identity from
``access.default_agent_user``, every action definition as a tool, the data
library as knowledge. Each subagent's block is also kept verbatim under
``extensions.agentforce``, so its routing and procedural instructions are part
of the version and survive a round trip.

Mapping rules are documented in ``docs/formats/agentforce.md``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..hashing import sha256_text
from ..model import (
    MARSHAL_NAMESPACE,
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
from .base import ReadOptions, ReadResult, Reader, build_record

FORMAT = "agentforce"
SUFFIX = ".agent"
_SUBAGENT = re.compile(r"^(start_agent|subagent|topic)\s+([A-Za-z_][\w]*)\s*:\s*$")
_KEY_VALUE = re.compile(r"^([A-Za-z_@][\w.@]*)\s*:\s*(.*)$")
_DECLARATION = re.compile(r"^(?:(mutable|linked)\s+)?([A-Za-z_][\w\[\]]*)(?:\s*=\s*(.+))?$")
_TARGET = re.compile(r"^([A-Za-z]+)://(.+)$")
_MARSHAL_COMMENT = re.compile(r"^#\s*" + re.escape(MARSHAL_NAMESPACE) + r":\s*(\{.*\})\s*$", re.MULTILINE)
_TYPES = {"string": "string", "number": "number", "integer": "integer", "boolean": "boolean", "object": "object",
          "date": "string", "datetime": "string", "currency": "number", "id": "string"}


@dataclass
class Node:
    indent: int
    text: str
    line: int
    children: list["Node"] = field(default_factory=list)


def parse_tree(source: str) -> tuple[list[Node], list[str]]:
    """Split Agent Script into an indentation tree. Comments and blank lines are dropped; raw lines are kept."""
    lines = source.splitlines()
    root = Node(-1, "", 0)
    stack = [root]
    for number, raw in enumerate(lines, 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" \t"))
        while stack[-1].indent >= indent:
            stack.pop()
        node = Node(indent, stripped, number)
        stack[-1].children.append(node)
        stack.append(node)
    return root.children, lines


def _scalar(text: str) -> Any:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        return text[1:-1].replace('\\"', '"')
    if text in ("True", "False"):
        return text == "True"
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    return text


def block_text(node: Node, lines: list[str]) -> str:
    """The raw source of a node's children, dedented, comments included."""
    if not node.children:
        return ""
    first = node.children[0].line - 1
    last = _last_line(node) - 1
    chunk = lines[first:last + 1]
    indent = min((len(l) - len(l.lstrip(" \t")) for l in chunk if l.strip()), default=0)
    return "\n".join(l[indent:] if l.strip() else "" for l in chunk).strip("\n")


def _last_line(node: Node) -> int:
    return _last_line(node.children[-1]) if node.children else node.line


def node_value(node: Node, lines: list[str]) -> Any:
    """A ``key: value`` node's value: a scalar, block literal text (``|``), or procedural block text (``->``)."""
    match = _KEY_VALUE.match(node.text)
    raw = match.group(2).strip() if match else ""
    if raw in ("|", "->"):
        text = block_text(node, lines)
        return text if raw == "|" else text
    return _scalar(raw)


def mapping(node: Node, lines: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for child in node.children:
        match = _KEY_VALUE.match(child.text)
        if not match:
            continue
        key, raw = match.group(1), match.group(2).strip()
        if raw in ("|", "->") or (child.children and raw == ""):
            out[key] = node_value(child, lines) if raw in ("|", "->") else mapping(child, lines)
        else:
            out[key] = _scalar(raw)
    return out


def _declaration(node: Node, lines: list[str]) -> tuple[str, dict[str, Any]]:
    """``name: [mutable|linked] type [= default]`` with indented attributes."""
    match = _KEY_VALUE.match(node.text)
    name = match.group(1) if match else node.text
    declared = _DECLARATION.match(match.group(2).strip()) if match else None
    attrs = mapping(node, lines)
    if declared:
        if declared.group(1):
            attrs["modifier"] = declared.group(1)
        attrs["type"] = declared.group(2)
        if declared.group(3):
            attrs["default"] = _scalar(declared.group(3))
    return name, attrs


def _schema(parameters: dict[str, dict[str, Any]], inputs: bool) -> Optional[dict[str, Any]]:
    if not parameters:
        return None
    props, required = {}, []
    for name, attrs in parameters.items():
        kind = str(attrs.get("type", "string"))
        item = {"type": "array", "items": {"type": _TYPES.get(kind[5:-1], "string")}} if kind.startswith("list[") \
            else {"type": _TYPES.get(kind, "string")}
        if attrs.get("description"):
            item["description"] = attrs["description"]
        props[name] = item
        if inputs and attrs.get("is_required") is True:
            required.append(name)
    schema: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


def target_ref(target: str) -> str:
    match = _TARGET.match(target)
    if not match:
        return f"tool/salesforce.{target}"
    scheme, name = match.groups()
    scheme = {"standardInvocableAction": "standard", "apex": "apex", "flow": "flow", "prompt": "prompt"}.get(scheme, scheme)
    return f"tool/salesforce.{scheme}.{name}"


def _is_placeholder(value: Any) -> bool:
    return not isinstance(value, str) or not value or ("<" in value and ">" in value)


class AgentforceReader(Reader):
    format = FORMAT
    platform = FORMAT

    @classmethod
    def detect(cls, path: Path) -> bool:
        return any(True for _ in cls._files(path))

    @staticmethod
    def _files(path: Path):
        if path.is_file():
            if path.suffix == SUFFIX:
                yield path
            return
        if path.is_dir():
            for file in sorted(path.rglob(f"*{SUFFIX}")):
                if not any(part in (".git", "node_modules", ".venv") for part in file.parts):
                    yield file

    def read(self, path: Path, options: ReadOptions | None = None) -> ReadResult:
        options = options or ReadOptions()
        result = ReadResult()
        root = path if path.is_dir() else path.parent
        entities: dict[str, Entity] = {}
        for file in self._files(path):
            try:
                source = file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            record = self.read_text(source, file.relative_to(root).as_posix(), options, result, entities)
            if record is not None:
                result.records.append(record)
        result.entities = sorted(entities.values(), key=lambda e: e.ref)
        return result

    def read_text(self, source: str, artifact: str, options: ReadOptions, result: ReadResult,
                  entities: dict[str, Entity]):
        nodes, lines = parse_tree(source)
        blocks = {n.text[:-1]: n for n in nodes if n.text.endswith(":") and " " not in n.text[:-1]}
        if "config" not in blocks:
            return None
        config = mapping(blocks["config"], lines)
        developer_name = config.get("developer_name")
        if not isinstance(developer_name, str):
            result.warnings.append(f"{artifact}: config.developer_name is missing; not read")
            return None
        marshal_match = _MARSHAL_COMMENT.search(source)
        marshal: dict[str, Any] = {}
        if marshal_match:
            import json

            try:
                marshal = json.loads(marshal_match.group(1))
            except ValueError:
                marshal = {}
        agent_id = str(marshal.get("agent") or developer_name)
        platform = str(marshal.get("platform") or FORMAT)

        def entity(kind: EntityKind, ref: str, display: Optional[str] = None) -> None:
            entities.setdefault(ref, Entity(ref=ref, kind=kind, platform=FORMAT, identifier=ref.split("/", 1)[1], display_name=display))

        spec = AgentSpec()
        extension: dict[str, Any] = {}
        if config.get("agent_label"):
            spec.display_name = Value[str].declared(str(config["agent_label"]), "config.agent_label")
        if config.get("description"):
            spec.description = Value[str].declared(redact(str(config["description"]))[0], "config.description")
        extension["config"] = {k: v for k, v in config.items() if k not in ("agent_label", "description")}

        system = mapping(blocks["system"], lines) if "system" in blocks else {}
        instructions = system.get("instructions")
        if isinstance(instructions, str) and instructions.strip():
            text, count = redact(instructions)
            if count:
                result.warnings.append(f"{artifact}: system.instructions: redacted {count} credential-like value(s)")
            spec.instructions = Instructions(hash=sha256_text(text), text=None if options.hash_only else text,
                                             provenance=Provenance.DECLARED, basis=f"{artifact}#system.instructions")
        if system.get("messages"):
            extension["messages"] = system["messages"]
        spec.model.requested = Value[str].unknown("chosen by the Agentforce reasoning engine")

        access = mapping(blocks["access"], lines) if "access" in blocks else {}
        agent_type = config.get("agent_type")
        if isinstance(access.get("default_agent_user"), str):
            spec.permissions.identity = Value[IdentityKind].declared(IdentityKind.SERVICE_PRINCIPAL, "access.default_agent_user")
            spec.permissions.principal = Value[str].declared(access["default_agent_user"], "access.default_agent_user")
        elif agent_type == "AgentforceEmployeeAgent":
            spec.permissions.identity = Value[IdentityKind].derived(
                IdentityKind.ON_BEHALF_OF_USER, "employee agents run as the signed-in user"
            )
        extra_access = {k: v for k, v in access.items() if k != "default_agent_user"}
        if extra_access:
            extension["access"] = extra_access

        if "knowledge" in blocks:
            knowledge = mapping(blocks["knowledge"], lines)
            extension["knowledge"] = knowledge
            library = knowledge.get("rag_feature_config_id")
            if not _is_placeholder(library):
                ref = f"knowledge/salesforce.data_library.{library}"
                spec.knowledge.append(KnowledgeUse(ref=ref, type="data_library",
                                                   scope=Value[Scope].derived(Scope.READ, "a data library is searched")))
                entity(EntityKind.KNOWLEDGE, ref)

        for name in ("variables", "language"):
            if name in blocks:
                extension[name] = block_text(blocks[name], lines)

        subagents: dict[str, Any] = {}
        for node in nodes:
            match = _SUBAGENT.match(node.text)
            if not match:
                continue
            kind, name = match.groups()
            subagents[name] = {"kind": kind, "source": block_text(node, lines)}
            for child in node.children:
                if child.text == "actions:":
                    for action in child.children:
                        use = self._action(action, lines, f"{artifact}#{name}.actions")
                        if use is not None and all(t.ref != use.ref for t in spec.tools):
                            spec.tools.append(use)
                            entity(EntityKind.TOOL, use.ref, use.name)
                elif child.text == "reasoning:":
                    for part in child.children:
                        if part.text == "actions:":
                            for action in part.children:
                                if "@utils.escalate" in action.text and all(t.ref != "tool/salesforce.escalate" for t in spec.tools):
                                    spec.tools.append(ToolUse(ref="tool/salesforce.escalate", name="escalate",
                                                              scope=Value[Scope].derived(Scope.EXECUTE, "hands the conversation to a human")))
                                    entity(EntityKind.TOOL, "tool/salesforce.escalate", "escalate")
        if subagents:
            extension["subagents"] = subagents
        else:
            result.warnings.append(f"{artifact}: no start_agent block")
        for node in nodes:
            key = node.text[:-1] if node.text.endswith(":") else None
            if key and " " not in key and key not in ("config", "system", "access", "knowledge", "variables", "language"):
                extension.setdefault("blocks", {})[key] = block_text(node, lines)
        spec.extensions[FORMAT] = extension

        return build_record(
            agent=agent_id,
            platform=platform,
            source=Source(format=FORMAT, artifact=artifact, detected_by="Agent Script file",
                          commit=options.commit, pipeline_run=options.pipeline_run),
            spec=spec,
        )

    def _action(self, node: Node, lines: list[str], where: str) -> Optional[ToolUse]:
        match = _KEY_VALUE.match(node.text)
        if not match:
            return None
        name = match.group(1)
        attrs = mapping(node, lines)
        target = attrs.get("target")
        if not isinstance(target, str):
            return None
        inputs = {n: a for n, a in (_declaration(c, lines) for c in self._section(node, "inputs"))}
        outputs = {n: a for n, a in (_declaration(c, lines) for c in self._section(node, "outputs"))}
        use = ToolUse(
            ref=target_ref(target),
            name=name,
            description=attrs.get("description") if isinstance(attrs.get("description"), str) else None,
            input_schema=_schema(inputs, inputs=True),
            output_schema=_schema(outputs, inputs=False),
        )
        if isinstance(attrs.get("require_user_confirmation"), bool):
            use.requires_confirmation = Value[bool].declared(attrs["require_user_confirmation"], f"{where}.{name}")
        return use

    @staticmethod
    def _section(node: Node, name: str) -> list[Node]:
        for child in node.children:
            if child.text == f"{name}:":
                return child.children
        return []
