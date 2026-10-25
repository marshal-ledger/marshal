"""Writer for AWS CloudFormation: one ``AWS::Bedrock::Agent`` per template.

The writer produces the agent resource, not its surrounding infrastructure.
IAM roles, executor Lambdas and knowledge bases are referenced, not created:
the role is a template parameter unless the record names an ARN, and tools
become function definitions in a ``RETURN_CONTROL`` action group, which the
calling application executes.
"""

from __future__ import annotations

import re
from typing import Any

from ..model import MARSHAL_NAMESPACE, AgentVersion
from ..readers.base import slug
from ..readers.cloudformation import AGENT, FORMAT
from .base import Writer, Written

_NAME = re.compile(r"[^0-9a-zA-Z_-]+")
_JSON_TYPES = {"string", "number", "integer", "boolean", "array"}
_MIN_INSTRUCTION = 40
_MAX_DESCRIPTION = 200
_BEDROCK_MODEL = re.compile(r"^(arn:aws|[a-z]{2}\.)?[a-z0-9-]*\.?(anthropic|amazon|meta|mistral|cohere|ai21|deepseek|writer|openai|qwen)\.")


def _name(text: str, limit: int = 100) -> str:
    """A Bedrock name: letters, digits, hyphens and underscores."""
    name = _NAME.sub("-", text).strip("-_") or "agent"
    return re.sub(r"([_-])[_-]+", r"\1", name)[:limit]


def _logical(text: str) -> str:
    parts = re.split(r"[^0-9a-zA-Z]+", text)
    name = "".join(p[:1].upper() + p[1:] for p in parts if p) or "Agent"
    return name if name[0].isalpha() else f"Agent{name}"


class CloudFormationWriter(Writer):
    format = FORMAT
    suffix = ".template.yaml"
    reader_format = FORMAT
    LOSSES = {
        "permissions.grants[*]": "IAM permissions live on the agent's role, which this writer references but does not create",
        "permissions.access[*]": "who may invoke the agent is governed by IAM policies outside the agent resource",
        "permissions.principal": "the agent's role is a template parameter, set at deploy time",
        "permissions.identity": "the agent's role is a template parameter, set at deploy time",
        "tools[*].scope": "Bedrock function definitions don't declare a scope; it is derived from an executor Lambda's role",
        "tools[*].reversible": "Bedrock function definitions have no reversibility field",
        "tools[*].output_schema": "Bedrock function definitions have no output schema",
        "tools[agent/*]": "collaborators are referenced by alias ARN, which only exists after the collaborator is deployed",
        "knowledge[*]": "only Bedrock knowledge bases, by id, can be attached",
        "knowledge[*].name": "knowledge bases are referenced by id; their name is on the knowledge base resource",
        "knowledge[*].type": "knowledge bases are referenced by id; their type is on the knowledge base resource",
        "knowledge[*].scope": "Bedrock agents only retrieve from knowledge bases",
        "model.parameters.*": "inference parameters are set through prompt override configuration, per prompt type",
        "model.resolved": "the template names the requested model only",
        "model.provider": "the provider is implied by the Bedrock model id",
    }
    MECHANICS = ("extensions.bedrock.resource_type",)

    def write(self, record: AgentVersion) -> Written:
        spec = record.spec
        notes: dict[str, str] = {}
        warnings: list[str] = []
        ext: dict[str, Any] = dict(spec.extensions.get("bedrock", {}))
        for platform in spec.extensions:
            if platform != "bedrock":
                notes[f"extensions.{platform}.*"] = f"{platform}-specific; no Bedrock equivalent"

        name = str(spec.display_name.value or record.agent.rsplit("/", 1)[-1])
        agent_name = _name(name)
        if not spec.display_name.is_known:
            notes["display_name"] = "Bedrock requires an agent name; one was derived from the agent id"
        elif agent_name != name:
            notes["display_name"] = "Bedrock agent names allow only letters, digits, hyphens and underscores"
        logical = _logical(record.agent.rsplit("/", 1)[-1])
        props: dict[str, Any] = {"AgentName": agent_name}

        if spec.description.is_known:
            description = str(spec.description.value)
            if len(description) > _MAX_DESCRIPTION:
                description = description[: _MAX_DESCRIPTION - 1] + "…"
                notes["description"] = f"Bedrock agent descriptions are limited to {_MAX_DESCRIPTION} characters"
            props["Description"] = description

        if spec.model.requested.is_known:
            props["FoundationModel"] = spec.model.requested.value
            if not _BEDROCK_MODEL.search(str(spec.model.requested.value)):
                warnings.append(f"{spec.model.requested.value} is not a Bedrock model id; choose one before deploying")
        else:
            warnings.append("the source names no model; set FoundationModel before deploying")

        if spec.instructions.text:
            props["Instruction"] = spec.instructions.text
            if len(spec.instructions.text) < _MIN_INSTRUCTION:
                warnings.append(f"Bedrock requires instructions of at least {_MIN_INSTRUCTION} characters")
        elif spec.instructions.hash:
            notes["instructions"] = "instruction text is not available (hash-only source)"

        parameters: dict[str, Any] = {}
        principal = spec.permissions.principal.value if spec.permissions.principal.is_known else None
        if principal and str(principal).startswith("arn:aws:iam::"):
            props["AgentResourceRoleArn"] = principal
        else:
            parameters["AgentResourceRoleArn"] = {"Type": "String", "Description": "ARN of the IAM role the agent runs as"}
            props["AgentResourceRoleArn"] = {"Ref": "AgentResourceRoleArn"}

        # Tools: built-in groups, then one RETURN_CONTROL group of functions.
        tool_refs: dict[str, str] = {}
        groups: list[dict[str, Any]] = []
        functions: list[dict[str, Any]] = []
        used: set[str] = set()
        for use in spec.tools:
            base = f"tools[{use.ref}]"
            if use.ref == "tool/bedrock.user_input":
                groups.append({"ActionGroupName": use.name or "UserInputAction", "ParentActionGroupSignature": "AMAZON.UserInput"})
                continue
            if use.ref == "tool/bedrock.code_interpreter":
                groups.append({"ActionGroupName": use.name or "CodeInterpreterAction", "ParentActionGroupSignature": "AMAZON.CodeInterpreter"})
                continue
            if use.ref.startswith("agent/"):
                continue
            function_name = _name(use.name or use.ref.split("/", 1)[-1])
            if function_name != use.name:
                notes[f"{base}.name"] = "Bedrock function names allow only letters, digits, hyphens and underscores"
            while function_name in used:
                function_name += "_"
            used.add(function_name)
            function: dict[str, Any] = {"Name": function_name}
            if use.description:
                function["Description"] = use.description
            parameters_ = self._parameters(use.input_schema, base, notes)
            if parameters_:
                function["Parameters"] = parameters_
            if use.requires_confirmation.is_known:
                function["RequireConfirmation"] = "ENABLED" if use.requires_confirmation.value else "DISABLED"
            functions.append(function)
            tool_refs[f"tools.{function_name}"] = use.ref
        if functions:
            groups.append({
                "ActionGroupName": "tools",
                "Description": "Tools executed by the calling application",
                "ActionGroupExecutor": {"CustomControl": "RETURN_CONTROL"},
                "FunctionSchema": {"Functions": functions},
            })
        if groups:
            props["ActionGroups"] = groups

        knowledge_bases = []
        for item in spec.knowledge:
            if not item.ref.startswith("knowledge/bedrock.knowledge_base."):
                continue
            kb_id = item.ref.removeprefix("knowledge/bedrock.knowledge_base.")
            knowledge_bases.append({
                "KnowledgeBaseId": kb_id,
                "Description": item.description or item.name or f"Knowledge base {kb_id}",
                "KnowledgeBaseState": "ENABLED",
            })
            if not item.description:
                notes[f"knowledge[{item.ref}].description"] = "Bedrock requires a knowledge base description; one was generated"
        if knowledge_bases:
            props["KnowledgeBases"] = knowledge_bases

        for label, key in (("guardrail", "GuardrailConfiguration"), ("memory", "MemoryConfiguration"),
                           ("idle_session_ttl", "IdleSessionTTLInSeconds"), ("orchestration", "OrchestrationType"),
                           ("custom_orchestration", "CustomOrchestration"), ("prompt_override", "PromptOverrideConfiguration"),
                           ("collaboration", "AgentCollaboration")):
            if label in ext:
                props[key] = ext[label]
        for label in ("aliases", "external_schemas", "external_collaborators", "disabled_action_groups",
                      "managed_policies", "conditional"):
            if label in ext:
                notes[f"extensions.bedrock.{label}"] = "describes the surrounding stack, which this writer does not generate"

        metadata: dict[str, Any] = {"agent": record.agent}
        if record.platform != "bedrock":
            metadata["platform"] = record.platform
        if tool_refs:
            # Function refs are re-derived from the agent id when they match; only foreign refs need keeping.
            prefix = f"tool/bedrock.{record.agent.replace('/', '.')}.tools."
            kept = {k: v for k, v in tool_refs.items() if v != prefix + k.split(".", 1)[1]}
            if kept:
                metadata["tool_refs"] = kept

        resource = {"Type": AGENT, "Metadata": {MARSHAL_NAMESPACE: metadata}, "Properties": props}
        template: dict[str, Any] = {"AWSTemplateFormatVersion": "2010-09-09"}
        if spec.description.is_known:
            template["Description"] = props.get("Description")
        if parameters:
            template["Parameters"] = parameters
        template["Resources"] = {logical: resource}
        return Written(document=template, notes=notes, warnings=warnings)

    def _parameters(self, schema: Any, base: str, notes: dict[str, str]) -> dict[str, Any]:
        if not isinstance(schema, dict):
            return {}
        required = set(schema.get("required") or [])
        out = {}
        for name, prop in (schema.get("properties") or {}).items():
            kind = prop.get("type") if isinstance(prop, dict) else None
            if kind not in _JSON_TYPES:
                notes[f"{base}.input_schema"] = "Bedrock function parameters are flat and typed string, number, integer, boolean or array"
                kind = "string"
            elif isinstance(prop, dict) and set(prop) - {"type", "description"}:
                notes[f"{base}.input_schema"] = "Bedrock function parameters keep only a type and a description"
            entry: dict[str, Any] = {"Type": kind, "Required": name in required}
            if isinstance(prop, dict) and prop.get("description"):
                entry["Description"] = prop["description"]
            out[name] = entry
        return out
