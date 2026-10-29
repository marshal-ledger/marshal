"""Reader for AWS CloudFormation templates and CDK output: Bedrock Agents and AgentCore.

Reads ``AWS::Bedrock::Agent`` resources in full (instructions, model, action
groups, knowledge bases, guardrails, collaborators), AgentCore harnesses
(``AWS::BedrockAgentCore::Harness``: model, system prompt, tools, including
the tools of AgentCore gateways defined in the same template, and the
default shell and file tools that ``AllowedTools`` doesn't restrict) in full,
and ``AWS::BedrockAgentCore::Runtime`` resources at inventory level, since an
AgentCore runtime hosts agent code whose model and tools are not in the
template. IAM roles defined in the same template are resolved:

- the agent's own role gives its identity and grants;
- the role of each action group's executor Lambda gives that tool's scope.

Intrinsic functions are resolved where the template allows it. ``Ref`` to a
parameter uses the parameter's default, marked as derived, since a deploy can
override it. Other references stay as ``${...}`` tokens.

Mapping rules are documented in ``docs/formats/cloudformation.md``.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

import yaml

from ..hashing import sha256_text
from ..model import (
    MARSHAL_NAMESPACE,
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
from .python.frameworks import _HOSTED_RAW as _HOSTED_TOOLS
from .base import DocumentReader, ReadOptions, ReadResult, build_record

FORMAT = "cloudformation"
AGENT = "AWS::Bedrock::Agent"
ALIAS = "AWS::Bedrock::AgentAlias"
KNOWLEDGE_BASE = "AWS::Bedrock::KnowledgeBase"
RUNTIME = "AWS::BedrockAgentCore::Runtime"
RUNTIME_ENDPOINT = "AWS::BedrockAgentCore::RuntimeEndpoint"
HARNESS = "AWS::BedrockAgentCore::Harness"
HARNESS_ENDPOINT = "AWS::BedrockAgentCore::HarnessEndpoint"
GATEWAY = "AWS::BedrockAgentCore::Gateway"
GATEWAY_TARGET = "AWS::BedrockAgentCore::GatewayTarget"
ROLE = "AWS::IAM::Role"
POLICY = "AWS::IAM::Policy"
FUNCTION = "AWS::Lambda::Function"
_AGENT_TYPES = {AGENT, RUNTIME, HARNESS}
_READ_METHODS = {"get", "head", "options"}
_BUILTIN_GROUPS = {
    "AMAZON.UserInput": ("tool/bedrock.user_input", Scope.READ, "asks the user for input"),
    "AMAZON.CodeInterpreter": ("tool/bedrock.code_interpreter", Scope.EXECUTE, "runs generated code"),
}
# Harness model configurations, and the provider each implies where the model id doesn't name one.
_HARNESS_MODELS = {"BedrockModelConfig": None, "OpenAiModelConfig": "openai", "GeminiModelConfig": "google",
                   "LiteLlmModelConfig": None}
_HARNESS_PARAMETERS = {"Temperature": "temperature", "TopP": "top_p", "TopK": "top_k", "MaxTokens": "max_tokens"}
# Tools every harness session has unless AllowedTools restricts them (AgentCore developer guide, "Tools").
HARNESS_BUILTINS = {
    "shell": (Scope.EXECUTE, "runs bash commands in the session's microVM"),
    "file_operations": (Scope.WRITE, "views, creates and edits files in the session's filesystem"),
}
_MODEL_VENDORS = re.compile(r"(?:^|[./:])(anthropic|amazon|meta|mistral|cohere|ai21|deepseek|writer|openai|qwen|stability)\.")


class _CfnLoader(yaml.SafeLoader):
    """YAML loader that turns CloudFormation short-form tags into their long form."""


def _intrinsic(loader: yaml.Loader, suffix: str, node: yaml.Node) -> Any:
    name = "Ref" if suffix == "Ref" else f"Fn::{suffix}"
    if isinstance(node, yaml.ScalarNode):
        value: Any = loader.construct_scalar(node)
        if suffix == "GetAtt" and isinstance(value, str):
            value = value.split(".", 1)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {name: value}


_CfnLoader.add_multi_constructor("!", _intrinsic)


def load_template(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return yaml.load(text, Loader=_CfnLoader)  # noqa: S506 - SafeLoader subclass


def action_scope(action: str) -> Scope:
    """Classify an IAM action by its verb."""
    if action == "*" or action.endswith(":*"):
        return Scope.ADMIN
    verb = action.split(":", 1)[-1]
    if re.match(r"(Get|List|Describe|Retrieve|Query|Search|Scan|Read|View|BatchGet|Lookup|Head)", verb):
        return Scope.READ
    if re.match(r"(Invoke|Execute|Start|Run|Converse|Call|Send|Publish|Apply)", verb):
        return Scope.EXECUTE
    return Scope.WRITE


_SCOPE_ORDER = [Scope.READ, Scope.EXECUTE, Scope.WRITE, Scope.ADMIN]


class _Template:
    """A CloudFormation template being read. Other sources that describe the same AWS resources,
    such as a Terraform plan, subclass it to name their own format and locations."""

    source_format = FORMAT

    def __init__(self, data: dict[str, Any], artifact: str):
        self.data = data
        self.artifact = artifact
        self.parameters: dict[str, Any] = data.get("Parameters") or {}
        self.resources: dict[str, Any] = data.get("Resources") or {}
        self.used_defaults: set[str] = set()

    def resource_type(self, logical: str) -> str:
        """The resource's type as the source names it, for ``detected_by``."""
        return str((self.resources.get(logical) or {}).get("Type"))

    def path(self, logical: str, prop: str = "") -> str:
        """Where a resource, or one of its properties, is in the source: the basis of values read from it."""
        return f"Resources.{logical}" + (f".Properties.{prop}" if prop else "")

    def of_type(self, kind: str) -> dict[str, dict[str, Any]]:
        return {k: v for k, v in self.resources.items() if isinstance(v, dict) and v.get("Type") == kind}

    def props(self, logical: str) -> dict[str, Any]:
        return (self.resources.get(logical) or {}).get("Properties") or {}

    def target(self, value: Any) -> Optional[str]:
        """The logical id a ``Ref`` or ``GetAtt`` points to, if it is a resource in this template."""
        if isinstance(value, dict):
            if "Ref" in value and value["Ref"] in self.resources:
                return value["Ref"]
            if "Fn::GetAtt" in value:
                ref = value["Fn::GetAtt"]
                ref = ref.split(".", 1) if isinstance(ref, str) else ref
                if isinstance(ref, list) and ref and ref[0] in self.resources:
                    return ref[0]
        return None

    def resolve(self, value: Any) -> Any:
        """Resolve intrinsics to plain values where possible; unresolved parts become ``${...}`` tokens."""
        if isinstance(value, list):
            return [self.resolve(v) for v in value]
        if not isinstance(value, dict):
            return value
        if len(value) != 1:
            return {k: self.resolve(v) for k, v in value.items()}
        (key, arg), = value.items()
        if key == "Ref":
            if isinstance(arg, str) and arg in self.parameters:
                default = (self.parameters[arg] or {}).get("Default")
                if default is not None:
                    self.used_defaults.add(arg)
                    return default
            return f"${{{arg}}}"
        if key == "Fn::GetAtt":
            parts = arg.split(".", 1) if isinstance(arg, str) else arg
            return "${" + ".".join(str(p) for p in parts) + "}"
        if key == "Fn::Sub":
            template, names = (arg, {}) if isinstance(arg, str) else (arg[0], arg[1] if len(arg) > 1 else {})

            def substitute(match: re.Match) -> str:
                name = match.group(1)
                if name in names:
                    resolved = self.resolve(names[name])
                    return resolved if isinstance(resolved, str) else match.group(0)
                if name in self.parameters:
                    resolved = self.resolve({"Ref": name})
                    return str(resolved)
                return match.group(0)

            return re.sub(r"\$\{([^}!]+)\}", substitute, template) if isinstance(template, str) else template
        if key == "Fn::Join" and isinstance(arg, list) and len(arg) == 2:
            parts = self.resolve(arg[1])
            if isinstance(parts, list) and all(isinstance(p, (str, int, float)) for p in parts):
                return str(arg[0]).join(str(p) for p in parts)
        if key == "Fn::ImportValue":
            # Another stack's export: its name identifies the value across templates.
            name = self.resolve(arg)
            return f"${{import:{name}}}" if isinstance(name, str) else "${import}"
        if key.startswith("Fn::") or key == "Condition":
            return f"${{{key}}}"
        return {key: self.resolve(arg)}

    def condition(self, name: str, depth: int = 0) -> Optional[bool]:
        """Evaluate a template condition with parameter defaults, or None if it can't be decided."""
        definition = (self.data.get("Conditions") or {}).get(name)
        return self._truth(definition, depth) if definition is not None and depth < 20 else None

    def _truth(self, node: Any, depth: int) -> Optional[bool]:
        if not isinstance(node, dict) or len(node) != 1:
            return None
        (key, arg), = node.items()
        if key == "Condition":
            return self.condition(arg, depth + 1)
        if key == "Fn::Equals" and isinstance(arg, list) and len(arg) == 2:
            left, right = (self.resolve(a) for a in arg)
            if any(isinstance(v, str) and "${" in v for v in (left, right)):
                return None
            return str(left) == str(right)
        if key == "Fn::Not" and isinstance(arg, list) and arg:
            inner = self._truth(arg[0], depth + 1)
            return None if inner is None else not inner
        if key in ("Fn::And", "Fn::Or") and isinstance(arg, list):
            values = [self._truth(a, depth + 1) for a in arg]
            if None in values:
                return None
            return all(values) if key == "Fn::And" else any(values)
        return None

    def items(self, value: Any, label: str, conditions: dict[str, str]) -> list[dict[str, Any]]:
        """The mappings in a list property. ``Fn::If`` takes the branch selected by parameter
        defaults when the condition can be decided, else both branches."""
        if isinstance(value, dict) and "Fn::If" in value and isinstance(value["Fn::If"], list) and len(value["Fn::If"]) == 3:
            condition, when_true, when_false = value["Fn::If"]
            decided = self.condition(condition)
            if decided is None:
                conditions[label] = f"depends on template condition {condition}"
                return self.items(when_true, label, conditions) + self.items(when_false, label, conditions)
            conditions[label] = f"template condition {condition} is {str(decided).lower()} with default parameters; a deploy may change it"
            return self.items(when_true if decided else when_false, label, conditions)
        if isinstance(value, list):
            out = []
            for item in value:
                out += self.items(item, label, conditions) if isinstance(item, dict) and "Fn::If" in item else (
                    [item] if isinstance(item, dict) and "Ref" not in item else [])
            return out
        return []

    def string(self, value: Any) -> Optional[str]:
        resolved = self.resolve(value)
        return resolved if isinstance(resolved, str) else None

    def role_grants(self, logical: str) -> tuple[list[Grant], list[str]]:
        """Grants from an IAM role's inline policies and attached ``AWS::IAM::Policy`` resources."""
        props = self.props(logical)
        documents = [p.get("PolicyDocument") for p in props.get("Policies") or []]
        for policy in self.of_type(POLICY).values():
            roles = (policy.get("Properties") or {}).get("Roles") or []
            if any(self.target(r) == logical for r in roles):
                documents.append((policy.get("Properties") or {}).get("PolicyDocument"))
        grants: list[Grant] = []
        for document in documents:
            statements = (document or {}).get("Statement") or []
            for statement in statements if isinstance(statements, list) else [statements]:
                if not isinstance(statement, dict) or statement.get("Effect") != "Allow":
                    continue
                actions = statement.get("Action") or []
                resources = statement.get("Resource") or ["*"]
                for action in actions if isinstance(actions, list) else [actions]:
                    for resource in resources if isinstance(resources, list) else [resources]:
                        target = self.string(resource) or str(self.resolve(resource))
                        grants.append(Grant(
                            target=f"other/aws.{target}", privilege=str(action), scope=action_scope(str(action)),
                            basis=f"{self.artifact}#{self.path(logical)}",
                        ))
        managed = [self.string(a) or str(a) for a in props.get("ManagedPolicyArns") or []]
        return grants, managed


# Values Bedrock stores when an agent doesn't set them, seen on real deploys. A value equal to
# its default is recorded as absent, so an agent records the same whether its source states
# the default or not, and a Terraform plan (which leaves unset values out) agrees with the
# state after apply (which has them filled in).
BEDROCK_DEFAULTS = {"IdleSessionTTLInSeconds": 600, "AgentCollaboration": "DISABLED", "OrchestrationType": "DEFAULT"}


def _is_default(key: str, value: Any) -> bool:
    return key in BEDROCK_DEFAULTS and str(value) == str(BEDROCK_DEFAULTS[key])


def _provider(model: str) -> Optional[str]:
    match = _MODEL_VENDORS.search(model)
    return match.group(1) if match else None


def _openapi_tools(text: str, base_ref: str) -> Optional[list[ToolUse]]:
    try:
        spec = json.loads(text)
    except ValueError:
        try:
            spec = yaml.safe_load(text)
        except yaml.YAMLError:
            return None
    if not isinstance(spec, dict):
        return None
    tools = []
    for path, operations in (spec.get("paths") or {}).items():
        for method, operation in (operations or {}).items():
            if method.lower() not in ("get", "put", "post", "delete", "patch", "head", "options") or not isinstance(operation, dict):
                continue
            name = operation.get("operationId") or f"{method.upper()} {path}"
            schema = None
            body = (((operation.get("requestBody") or {}).get("content") or {}).get("application/json") or {}).get("schema")
            if isinstance(body, dict):
                schema = body
            elif operation.get("parameters"):
                props = {p["name"]: p.get("schema", {}) for p in operation["parameters"] if isinstance(p, dict) and "name" in p}
                required = [p["name"] for p in operation["parameters"] if isinstance(p, dict) and p.get("required")]
                schema = {"type": "object", "properties": props, **({"required": required} if required else {})}
            read = method.lower() in _READ_METHODS
            tools.append(ToolUse(
                ref=f"{base_ref}.{name}",
                name=str(name),
                description=operation.get("description") or operation.get("summary"),
                input_schema=schema,
                scope=Value[Scope].derived(Scope.READ if read else Scope.WRITE, f"HTTP {method.upper()} {path}"),
                reversible=Value[bool].derived(True, "read-only HTTP method") if read else Value[bool].unknown(),
            ))
    return tools


class CloudFormationReader(DocumentReader):
    format = FORMAT
    platform = "bedrock"

    @classmethod
    def matches(cls, data: Any) -> bool:
        resources = data.get("Resources") if isinstance(data, dict) else None
        return isinstance(resources, dict) and any(
            isinstance(r, dict) and r.get("Type") in _AGENT_TYPES for r in resources.values()
        )

    @classmethod
    def detect(cls, path):  # type: ignore[override]
        from .base import iter_document_files

        if not path.exists():
            return False
        for file in iter_document_files(path):
            try:
                text = file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if any(t in text for t in _AGENT_TYPES):
                try:
                    if cls.matches(load_template(text)):
                        return True
                except yaml.YAMLError:
                    continue
        return False

    def read(self, path, options=None):  # type: ignore[override]
        from .base import iter_document_files

        result = ReadResult()
        root = path if path.is_dir() else path.parent
        for file in iter_document_files(path):
            try:
                text = file.read_text(encoding="utf-8")
                if not any(t in text for t in _AGENT_TYPES):
                    continue
                data = load_template(text)
            except (OSError, UnicodeDecodeError, yaml.YAMLError):
                continue
            if self.matches(data):
                part = self.read_data(data, file.relative_to(root).as_posix(), options)
                result.records += part.records
                result.skipped += part.skipped
                result.warnings += part.warnings
                known = {e.ref for e in result.entities}
                result.entities += [e for e in part.entities if e.ref not in known]
        return result

    def read_data(self, data: Any, artifact: str, options: ReadOptions | None = None) -> ReadResult:
        return self.read_template(_Template(data, artifact), options)

    def read_template(self, template: _Template, options: ReadOptions | None = None) -> ReadResult:
        """Records for the Bedrock agents and AgentCore runtimes and harnesses in a template, or in another
        source translated into one (see :mod:`marshal_ledger.readers.terraform`)."""
        options = options or ReadOptions()
        artifact = template.artifact
        result = ReadResult()
        entities: dict[str, Entity] = {}
        stem = artifact.rsplit(".", 1)[0]
        agent_ids = {
            logical: self._marshal(resource).get("agent") or f"{stem}/{logical}"
            for logical, resource in template.of_type(AGENT).items()
        }
        alias_agents = {}
        for logical, alias in template.of_type(ALIAS).items():
            target = template.target((alias.get("Properties") or {}).get("AgentId"))
            if target:
                alias_agents[logical] = target

        for logical in sorted(template.of_type(AGENT)):
            result.records.append(self._agent(template, logical, agent_ids, alias_agents, options, result, entities))
        for logical in sorted(template.of_type(RUNTIME)):
            result.records.append(self._runtime(template, logical, stem, options, result, entities))
        for logical in sorted(template.of_type(HARNESS)):
            result.records.append(self._harness(template, logical, stem, options, result, entities))
        result.entities = sorted(entities.values(), key=lambda e: e.ref)
        return result

    # -- shared ----------------------------------------------------------

    @staticmethod
    def _marshal(resource: dict[str, Any]) -> dict[str, Any]:
        value = (resource.get("Metadata") or {}).get(MARSHAL_NAMESPACE)
        return value if isinstance(value, dict) else {}

    def _value(self, template: _Template, raw: Any, where: str) -> Value[str]:
        template.used_defaults.clear()
        resolved = template.resolve(raw)
        if not isinstance(resolved, str):
            return Value[str].unknown(f"{where} is not a string")
        if "${" in resolved:
            return Value[str].unknown(f"{where} is set at deploy time: {resolved}")
        if template.used_defaults:
            names = ", ".join(sorted(template.used_defaults))
            return Value[str].derived(resolved, f"{where}: default of template parameter {names}; a deploy may override it")
        return Value[str].declared(resolved, where)

    def _identity(self, template: _Template, role_value: Any, where: str, permissions: Permissions,
                  extension: dict[str, Any]) -> None:
        role = template.target(role_value)
        if role is None:
            arn = template.string(role_value)
            if arn and "${" not in arn:
                permissions.identity = Value[IdentityKind].declared(IdentityKind.SERVICE_PRINCIPAL, where)
                permissions.principal = Value[str].declared(arn, where)
            elif role_value is not None:
                permissions.identity = Value[IdentityKind].declared(IdentityKind.SERVICE_PRINCIPAL, where)
                permissions.principal = Value[str].unknown(f"{where} is set at deploy time")
            return
        permissions.identity = Value[IdentityKind].declared(IdentityKind.SERVICE_PRINCIPAL, f"{where}: IAM role {role}")
        name = template.string(template.props(role).get("RoleName"))
        permissions.principal = (
            Value[str].declared(name, template.path(role, "RoleName")) if name and "${" not in name
            else Value[str].declared(f"iam-role/{role}", template.path(role))
        )
        grants, managed = template.role_grants(role)
        permissions.grants += grants
        if managed:
            extension["managed_policies"] = managed

    # -- Bedrock Agents ----------------------------------------------------

    def _agent(self, template, logical, agent_ids, alias_agents, options, result, entities):
        props = template.props(logical)
        agent_id = agent_ids[logical]
        marshal = self._marshal(template.resources[logical])
        tool_refs: dict[str, str] = marshal.get("tool_refs") or {}
        knowledge_refs: dict[str, str] = marshal.get("knowledge_refs") or {}
        spec = AgentSpec()
        extension: dict[str, Any] = {"resource_type": AGENT}
        bindings: dict[str, Any] = {}

        def entity(kind: EntityKind, ref: str, display: Optional[str] = None) -> None:
            entities.setdefault(ref, Entity(ref=ref, kind=kind, platform="bedrock", identifier=ref.split("/", 1)[1], display_name=display))

        spec.display_name = self._value(template, props.get("AgentName"), template.path(logical, "AgentName"))
        if props.get("Description") is not None:
            spec.description = self._value(template, props["Description"], template.path(logical, "Description"))
            if isinstance(spec.description.value, str):
                spec.description.value = redact(spec.description.value)[0]

        model = self._value(template, props.get("FoundationModel"), template.path(logical, "FoundationModel"))
        spec.model.requested = model
        if model.is_known and (vendor := _provider(str(model.value))):
            spec.model.provider = Value[str].derived(vendor, f"model id {model.value}")

        instruction = self._value(template, props.get("Instruction"), template.path(logical, "Instruction"))
        if instruction.is_known:
            text, count = redact(str(instruction.value))
            if count:
                result.warnings.append(f"{template.artifact}: {logical}: redacted {count} credential-like value(s)")
            spec.instructions = Instructions(
                hash=sha256_text(text), text=None if options.hash_only else text,
                provenance=instruction.provenance, basis=instruction.basis,
            )
        else:
            spec.instructions = Instructions(basis=instruction.basis)
        if props.get("PromptOverrideConfiguration"):
            extension["prompt_override"] = template.resolve(props["PromptOverrideConfiguration"])

        # Action groups
        disabled = []
        conditions: dict[str, str] = {}
        for group in template.items(props.get("ActionGroups"), "ActionGroups", conditions):
            name = template.string(group.get("ActionGroupName")) or "action-group"
            if str(template.resolve(group.get("ActionGroupState", "ENABLED"))) == "DISABLED":
                disabled.append(name)
                continue
            signature = group.get("ParentActionGroupSignature")
            if signature in _BUILTIN_GROUPS:
                ref, scope, rule = _BUILTIN_GROUPS[signature]
                spec.tools.append(ToolUse(ref=ref, name=name, scope=Value[Scope].derived(scope, f"{signature} {rule}")))
                entity(EntityKind.TOOL, ref, name)
                continue
            base_ref = f"tool/bedrock.{agent_id.replace('/', '.')}.{name}"
            executor_scope = self._executor_scope(template, group.get("ActionGroupExecutor") or {})
            executor = self._executor(template, group.get("ActionGroupExecutor") or {})
            schema = group.get("ApiSchema") or {}
            functions = (group.get("FunctionSchema") or {}).get("Functions")
            tools: list[ToolUse] = []
            if schema.get("Payload"):
                payload = template.string(schema["Payload"])
                parsed = _openapi_tools(payload, base_ref) if payload else None
                if parsed is None:
                    result.warnings.append(f"{template.artifact}: {logical}: action group {name}: OpenAPI payload not parsed")
                tools = parsed or []
            elif functions:
                for function in functions:
                    params = function.get("Parameters") or {}
                    schema_ = {
                        "type": "object",
                        "properties": {k: {"type": str(v.get("Type", "string")).lower(), **({"description": v["Description"]} if v.get("Description") else {})} for k, v in params.items()},
                    }
                    required = [k for k, v in params.items() if v.get("Required")]
                    if required:
                        schema_["required"] = required
                    use = ToolUse(ref=f"{base_ref}.{function.get('Name')}", name=function.get("Name"),
                                  description=function.get("Description"), input_schema=schema_ if params else None)
                    confirm = function.get("RequireConfirmation")
                    if confirm in ("ENABLED", "DISABLED"):
                        use.requires_confirmation = Value[bool].declared(confirm == "ENABLED", template.path(logical, f"ActionGroups[{name}]"))
                    tools.append(use)
            else:
                use = ToolUse(ref=base_ref, name=name, description=template.string(group.get("Description")))
                if schema.get("S3"):
                    extension.setdefault("external_schemas", {})[name] = template.resolve(schema["S3"])
                    result.warnings.append(f"{template.artifact}: {logical}: action group {name}: OpenAPI schema is in S3 and was not read")
                tools = [use]
            for use in tools:
                if executor_scope and not use.scope.is_known:
                    use.scope = executor_scope
                use.ref = tool_refs.get(f"{name}.{use.name}" if use.ref != base_ref else name, use.ref)
                if executor:
                    # Environment-bound, like the function itself, so it stays out of the version hash.
                    bindings.setdefault("executors", {})[use.ref] = executor
                spec.tools.append(use)
                entity(EntityKind.TOOL, use.ref, use.name)

        if disabled:
            extension["disabled_action_groups"] = disabled

        for collaborator in template.items(props.get("AgentCollaborators"), "AgentCollaborators", conditions):
            alias = template.target(((collaborator.get("AgentDescriptor") or {}).get("AliasArn")))
            target = alias_agents.get(alias) if alias else None
            name = template.string(collaborator.get("CollaboratorName"))
            if target:
                ref = f"agent/{agent_ids[target]}"
                spec.tools.append(ToolUse(ref=ref, name=name, description=template.string(collaborator.get("CollaborationInstruction"))))
                entity(EntityKind.AGENT, ref, name)
            else:
                extension.setdefault("external_collaborators", []).append(name)
        if props.get("AgentCollaboration") and not _is_default("AgentCollaboration", template.resolve(props["AgentCollaboration"])):
            extension["collaboration"] = template.resolve(props["AgentCollaboration"])

        for kb in template.items(props.get("KnowledgeBases"), "KnowledgeBases", conditions):
            target = template.target(kb.get("KnowledgeBaseId"))
            if target:
                kb_props = template.props(target)
                ref = f"knowledge/bedrock.knowledge_base.{target}"
                kind = ((kb_props.get("KnowledgeBaseConfiguration") or {}).get("Type") or "knowledge_base")
                name = template.string(kb_props.get("Name"))
            else:
                kb_id = template.string(kb.get("KnowledgeBaseId")) or "unknown"
                ref = knowledge_refs.get(kb_id, f"knowledge/bedrock.knowledge_base.{kb_id}")
                kind, name = "knowledge_base", None
            if str(template.resolve(kb.get("KnowledgeBaseState", "ENABLED"))) == "DISABLED":
                continue
            spec.knowledge.append(KnowledgeUse(
                ref=ref, name=name, type=str(kind).lower(), description=template.string(kb.get("Description")),
                scope=Value[Scope].derived(Scope.READ, "Bedrock agents only retrieve from knowledge bases"),
            ))
            entity(EntityKind.KNOWLEDGE, ref, name)

        self._identity(template, props.get("AgentResourceRoleArn"), template.path(logical, "AgentResourceRoleArn"), spec.permissions, extension)
        for key, label in (("GuardrailConfiguration", "guardrail"), ("MemoryConfiguration", "memory"),
                           ("IdleSessionTTLInSeconds", "idle_session_ttl"), ("OrchestrationType", "orchestration"),
                           ("CustomOrchestration", "custom_orchestration")):
            if props.get(key) is not None and not _is_default(key, template.resolve(props[key])):
                extension[label] = template.resolve(props[key])
        if conditions:
            extension["conditional"] = conditions
        aliases = [a for a, target in alias_agents.items() if target == logical]
        if aliases:
            extension["aliases"] = sorted(template.string(template.props(a).get("AgentAliasName")) or a for a in aliases)
        if props.get("CustomerEncryptionKeyArn"):
            bindings["customer_encryption_key"] = template.resolve(props["CustomerEncryptionKeyArn"])
        spec.extensions["bedrock"] = extension
        return build_record(
            agent=agent_id, platform=marshal.get("platform") or "bedrock",
            source=Source(format=template.source_format, artifact=template.artifact, locator=template.path(logical),
                          detected_by=f"resource type {template.resource_type(logical)}", commit=options.commit,
                          pipeline_run=options.pipeline_run),
            spec=spec, bindings=bindings,
        )

    def _executor(self, template: _Template, executor: dict[str, Any]) -> Optional[str]:
        """The Lambda function that runs an action group, as something another stack could name too.

        A literal function ARN (without a version or alias), ``import:<export>`` for a function
        imported from another stack, or ``function:<name>`` for one in this template whose name is
        stated. A function whose name CloudFormation generates is local to this stack: None.
        """
        value = executor.get("Lambda")
        if value is None:
            return None
        function = template.target(value)
        if function is not None:
            if template.resources[function].get("Type") != FUNCTION:
                return None
            name = template.string(template.props(function).get("FunctionName"))
            return f"function:{name}" if name and "${" not in name else None
        resolved = template.string(value) or ""
        if match := re.fullmatch(r"\$\{import:([^}]+)\}", resolved):
            return f"import:{match.group(1)}"
        if match := re.fullmatch(r"(arn:[\w-]+:lambda:[\w-]+:\d{12}:function:[\w-]+)(:[\w$-]+)?", resolved):
            return match.group(1)
        return None

    def _executor_scope(self, template: _Template, executor: dict[str, Any]) -> Optional[Value[Scope]]:
        """The widest scope in the IAM role of an action group's executor Lambda, when it is in the template."""
        function = template.target(executor.get("Lambda"))
        if function is None or template.resources[function].get("Type") != FUNCTION:
            return None  # RETURN_CONTROL, or a Lambda defined elsewhere
        role = template.target(template.props(function).get("Role"))
        if role is None:
            return None
        grants, managed = template.role_grants(role)
        relevant = [g for g in grants if not g.privilege.startswith("logs:")]
        if not relevant:
            return None
        widest = max(relevant, key=lambda g: _SCOPE_ORDER.index(g.scope))
        return Value[Scope].derived(
            widest.scope, f"widest action in the IAM role {role} of executor Lambda {function}: {widest.privilege}"
        )

    # -- AgentCore runtimes ------------------------------------------------

    def _runtime(self, template, logical, stem, options, result, entities):
        props = template.props(logical)
        spec = AgentSpec()
        extension: dict[str, Any] = {"resource_type": RUNTIME}
        spec.display_name = self._value(template, props.get("AgentRuntimeName"), template.path(logical, "AgentRuntimeName"))
        if props.get("Description") is not None:
            spec.description = self._value(template, props["Description"], template.path(logical, "Description"))
        spec.model.requested = Value[str].unknown("set in the runtime's agent code")
        spec.instructions = Instructions(basis="set in the runtime's agent code")
        self._identity(template, props.get("RoleArn"), template.path(logical, "RoleArn"), spec.permissions, extension)
        artifact = template.resolve(props.get("AgentRuntimeArtifact") or {})
        if artifact:
            extension["artifact"] = artifact
        for key, label in (("ProtocolConfiguration", "protocol"), ("NetworkConfiguration", "network"),
                           ("AuthorizerConfiguration", "authorizer"), ("LifecycleConfiguration", "lifecycle")):
            if props.get(key) is not None:
                extension[label] = template.resolve(props[key])
        if props.get("EnvironmentVariables"):
            extension["environment_variables"] = sorted(props["EnvironmentVariables"])
        endpoints = [template.string(template.props(e).get("Name")) or e for e, r in template.of_type(RUNTIME_ENDPOINT).items()
                     if template.target((r.get("Properties") or {}).get("AgentRuntimeId")) == logical]
        if endpoints:
            extension["endpoints"] = sorted(endpoints)
        spec.extensions["bedrock-agentcore"] = extension
        return build_record(
            agent=self._marshal(template.resources[logical]).get("agent") or f"{stem}/{logical}", platform="bedrock-agentcore",
            source=Source(format=template.source_format, artifact=template.artifact, locator=template.path(logical),
                          detected_by=f"resource type {template.resource_type(logical)}", commit=options.commit,
                          pipeline_run=options.pipeline_run),
            spec=spec,
        )

    # -- AgentCore harnesses -----------------------------------------------

    def _harness(self, template, logical, stem, options, result, entities):
        props = template.props(logical)
        marshal = self._marshal(template.resources[logical])
        agent_id = marshal.get("agent") or f"{stem}/{logical}"
        tool_refs: dict[str, str] = marshal.get("tool_refs") or {}
        local = f"tool/bedrock-agentcore.{agent_id.replace('/', '.')}"
        spec = AgentSpec()
        extension: dict[str, Any] = {"resource_type": HARNESS}
        bindings: dict[str, Any] = {}

        def entity(kind: EntityKind, ref: str, display: Optional[str] = None) -> None:
            entities.setdefault(ref, Entity(ref=ref, kind=kind, platform="bedrock-agentcore",
                                            identifier=ref.split("/", 1)[1], display_name=display))

        spec.display_name = self._value(template, props.get("HarnessName"), template.path(logical, "HarnessName"))

        model = props.get("Model") or {}
        kind = next((k for k in _HARNESS_MODELS if isinstance(model.get(k), dict)), None)
        if kind:
            config = model[kind]
            requested = self._value(template, config.get("ModelId"), template.path(logical, f"Model.{kind}.ModelId"))
            spec.model.requested = requested
            if _HARNESS_MODELS[kind]:
                spec.model.provider = Value[str].derived(_HARNESS_MODELS[kind], f"model configured with {kind}")
            elif requested.is_known and (vendor := _provider(str(requested.value))):
                spec.model.provider = Value[str].derived(vendor, f"model id {requested.value}")
            for key, name in _HARNESS_PARAMETERS.items():
                if config.get(key) is not None:
                    spec.model.parameters[name] = template.resolve(config[key])
            settings = {k: template.resolve(v) for k, v in config.items()
                        if k not in ("ModelId", "ApiKeyArn", *_HARNESS_PARAMETERS)}
            additional = dict(settings.pop("AdditionalParams", None) or {})
            if "guardrailConfig" in additional:
                # A Bedrock guardrail applied to every model call, as the Converse API's guardrailConfig.
                extension["guardrail"] = additional.pop("guardrailConfig")
            if isinstance(additional.get("tools"), list):
                # Tools the model provider runs itself, such as OpenAI's web search.
                extension["model_tools"] = additional.pop("tools")
                for tool in extension["model_tools"]:
                    hosted = _HOSTED_TOOLS.get(tool.get("type")) if isinstance(tool, dict) else None
                    if hosted:
                        ref, scope = hosted
                        spec.tools.append(ToolUse(ref=ref, name=str(tool["type"]), scope=Value[Scope].derived(
                            scope, f"{tool['type']}, run by the model provider ({template.path(logical, f'Model.{kind}.AdditionalParams.tools')})")))
                        entity(EntityKind.TOOL, ref, str(tool["type"]))
            if additional:
                settings["AdditionalParams"] = additional
            if settings or kind != "BedrockModelConfig":
                extension["model_config"] = {"type": kind, **settings}
            if config.get("ApiKeyArn") is not None:
                # A reference to a secret, bound to an account like the role.
                bindings["model_api_key"] = template.resolve(config["ApiKeyArn"])
        else:
            spec.model.requested = Value[str].unknown(f"{template.path(logical, 'Model')} names no model configuration")

        blocks = [template.string(b.get("Text")) for b in props.get("SystemPrompt") or [] if isinstance(b, dict)]
        where = template.path(logical, "SystemPrompt")
        if blocks and all(isinstance(b, str) and "${" not in b for b in blocks):
            text, count = redact("\n".join(blocks))
            if count:
                result.warnings.append(f"{template.artifact}: {logical}: redacted {count} credential-like value(s)")
            spec.instructions = Instructions(hash=sha256_text(text), text=None if options.hash_only else text,
                                             provenance=Provenance.DECLARED, basis=where)
        elif blocks:
            spec.instructions = Instructions(basis=f"{where} is set at deploy time")
        else:
            spec.instructions = Instructions(basis=f"{where} is not set")

        conditions: dict[str, str] = {}
        for tool in template.items(props.get("Tools"), "Tools", conditions):
            tool_type = template.string(tool.get("Type")) or "unknown"
            name = template.string(tool.get("Name")) or tool_type
            config = tool.get("Config") or {}
            uses: list[ToolUse] = []
            if tool_type == "remote_mcp":
                remote = config.get("RemoteMcp") or {}
                url = template.string(remote.get("Url"))
                ref = f"tool/mcp.{url}" if url and "${" not in url else f"{local}.{name}"
                uses = [ToolUse(ref=ref, name=name)]
                if remote.get("Headers"):
                    # Header values can hold tokens in plain text: only their names are recorded.
                    extension.setdefault("mcp_headers", {})[name] = sorted(remote["Headers"])
            elif tool_type == "agentcore_gateway":
                gateway = config.get("AgentCoreGateway") or {}
                uses = self._gateway_tools(template, gateway.get("GatewayArn"), name, local, stem, bindings, result)
                in_stack = template.target(gateway.get("GatewayArn"))
                if in_stack and template.resource_type(in_stack) == GATEWAY:
                    # The gateway's tools are listed one by one; this keeps which harness tool reaches them.
                    extension.setdefault("gateways", {})[name] = f"tool/bedrock-agentcore.gateway.{stem.replace('/', '.')}.{in_stack}"
                if gateway.get("OutboundAuth") is not None:
                    extension.setdefault("gateway_auth", {})[name] = template.resolve(gateway["OutboundAuth"])
            elif tool_type == "agentcore_browser":
                uses = [ToolUse(ref="tool/bedrock-agentcore.browser", name=name,
                                scope=Value[Scope].derived(Scope.EXECUTE, "AgentCore Browser operates a web browser"))]
                arn = (config.get("AgentCoreBrowser") or {}).get("BrowserArn")
                if arn is not None:
                    extension.setdefault("browsers", {})[name] = template.resolve(arn)
            elif tool_type == "agentcore_code_interpreter":
                uses = [ToolUse(ref="tool/bedrock-agentcore.code_interpreter", name=name,
                                scope=Value[Scope].derived(Scope.EXECUTE, "AgentCore Code Interpreter runs generated code"))]
                arn = (config.get("AgentCoreCodeInterpreter") or {}).get("CodeInterpreterArn")
                if arn is not None:
                    extension.setdefault("code_interpreters", {})[name] = template.resolve(arn)
            elif tool_type == "inline_function":
                function = config.get("InlineFunction") or {}
                schema = template.resolve(function.get("InputSchema"))
                uses = [ToolUse(ref=f"{local}.{name}", name=name, description=template.string(function.get("Description")),
                                input_schema=schema if isinstance(schema, dict) else None)]
            else:
                uses = [ToolUse(ref=f"{local}.{name}", name=name)]
                result.warnings.append(f"{template.artifact}: {logical}: tool {name} has type {tool_type}, which Marshal doesn't know")
            for use in uses:
                use.ref = tool_refs.get(use.name or name, use.ref) if len(uses) == 1 else use.ref
                spec.tools.append(use)
                entity(EntityKind.TOOL, use.ref, use.name)

        allowed = template.resolve(props["AllowedTools"]) if props.get("AllowedTools") is not None else None
        if allowed is not None:
            extension["allowed_tools"] = allowed
        for builtin, (scope, rule) in HARNESS_BUILTINS.items():
            basis = _builtin_allowed(builtin, allowed)
            if basis:
                ref = f"tool/bedrock-agentcore.{builtin}"
                spec.tools.append(ToolUse(ref=ref, name=builtin, scope=Value[Scope].derived(scope, f"{rule}; {basis}")))
                entity(EntityKind.TOOL, ref, builtin)

        self._identity(template, props.get("ExecutionRoleArn"), template.path(logical, "ExecutionRoleArn"), spec.permissions, extension)
        for key, label in (("Memory", "memory"), ("Hooks", "hooks"), ("Skills", "skills"), ("Environment", "environment"),
                           ("EnvironmentArtifact", "environment_artifact"), ("Truncation", "truncation"),
                           ("AuthorizerConfiguration", "authorizer"), ("MaxIterations", "max_iterations"),
                           ("MaxTokens", "max_tokens"), ("TimeoutSeconds", "timeout_seconds")):
            if props.get(key) is not None:
                extension[label] = template.resolve(props[key])
        if props.get("EnvironmentVariables"):
            extension["environment_variables"] = sorted(props["EnvironmentVariables"])
        if conditions:
            extension["conditional"] = conditions
        endpoints = [template.string(template.props(e).get("EndpointName")) or e for e, r in template.of_type(HARNESS_ENDPOINT).items()
                     if template.target((r.get("Properties") or {}).get("HarnessId")) == logical]
        if endpoints:
            extension["endpoints"] = sorted(endpoints)
        spec.extensions["bedrock-agentcore"] = extension
        return build_record(
            agent=agent_id, platform=marshal.get("platform") or "bedrock-agentcore",
            source=Source(format=template.source_format, artifact=template.artifact, locator=template.path(logical),
                          detected_by=f"resource type {template.resource_type(logical)}", commit=options.commit,
                          pipeline_run=options.pipeline_run),
            spec=spec, bindings=bindings,
        )

    def _gateway_tools(self, template: _Template, arn: Any, name: str, local: str, stem: str,
                       bindings: dict[str, Any], result: ReadResult) -> list[ToolUse]:
        """The tools a harness reaches through an AgentCore gateway: each target's tools when the
        gateway is defined in the template, else one tool standing for the whole gateway."""
        gateway = template.target(arn)
        if gateway is None or template.resource_type(gateway) != GATEWAY:
            resolved = template.string(arn) or ""
            if match := re.fullmatch(r"\$\{import:([^}]+)\}", resolved):
                return [ToolUse(ref=f"tool/bedrock-agentcore.gateway.import:{match.group(1)}", name=name)]
            if resolved.startswith("arn:") and "${" not in resolved:
                return [ToolUse(ref=f"tool/bedrock-agentcore.gateway.{resolved}", name=name)]
            return [ToolUse(ref=f"{local}.{name}", name=name)]
        base = f"tool/bedrock-agentcore.gateway.{stem.replace('/', '.')}.{gateway}"
        tools: list[ToolUse] = []
        for logical, resource in sorted(template.of_type(GATEWAY_TARGET).items()):
            props = resource.get("Properties") or {}
            if template.target(props.get("GatewayIdentifier")) != gateway:
                continue
            target = template.string(props.get("Name")) or logical
            target_ref = f"{base}.{target}"
            mcp = (props.get("TargetConfiguration") or {}).get("Mcp") or {}
            if isinstance(mcp.get("Lambda"), dict):
                executor = {"Lambda": mcp["Lambda"].get("LambdaArn")}
                scope = self._executor_scope(template, executor)
                function = self._executor(template, executor)
                inline = (mcp["Lambda"].get("ToolSchema") or {}).get("InlinePayload")
                found = []
                for definition in inline if isinstance(inline, list) else []:
                    found.append(ToolUse(ref=f"{target_ref}.{definition.get('Name')}", name=definition.get("Name"),
                                         description=template.string(definition.get("Description")),
                                         input_schema=_json_schema(template.resolve(definition.get("InputSchema"))),
                                         output_schema=_json_schema(template.resolve(definition.get("OutputSchema")))))
                if not found:
                    found = [ToolUse(ref=target_ref, name=target, description=template.string(props.get("Description")))]
                    result.warnings.append(f"{template.artifact}: {logical}: tool schema is not inline and was not read")
                for use in found:
                    if scope:
                        use.scope = scope
                    if function:
                        bindings.setdefault("executors", {})[use.ref] = function
                tools += found
            elif isinstance(mcp.get("McpServer"), dict) and template.string(mcp["McpServer"].get("Endpoint")):
                tools.append(ToolUse(ref=f"tool/mcp.{template.string(mcp['McpServer']['Endpoint'])}", name=target,
                                     description=template.string(props.get("Description"))))
            elif isinstance((mcp.get("OpenApiSchema") or {}).get("InlinePayload"), str):
                parsed = _openapi_tools(template.string(mcp["OpenApiSchema"]["InlinePayload"]) or "", target_ref)
                if parsed is None:
                    result.warnings.append(f"{template.artifact}: {logical}: OpenAPI payload not parsed")
                tools += parsed or [ToolUse(ref=target_ref, name=target)]
            else:
                tools.append(ToolUse(ref=target_ref, name=target, description=template.string(props.get("Description"))))
        return tools or [ToolUse(ref=base, name=name)]


def _builtin_allowed(name: str, allowed: Any) -> Optional[str]:
    """Why a harness default tool is available, or None when AllowedTools leaves it out."""
    if allowed is None:
        return "AllowedTools is not set, so every session has it"
    if not isinstance(allowed, list) or not all(isinstance(p, str) for p in allowed) or any("${" in p for p in allowed):
        return "AllowedTools is set at deploy time"

    def glob(pattern: str, text: str) -> bool:
        return re.fullmatch(".*".join(re.escape(part) for part in pattern.split("*")), text) is not None

    for pattern in allowed:
        if pattern.startswith("@"):
            server, _, tool = pattern[1:].partition("/")
            hit = glob(server, "builtin") and (not tool or glob(tool, name))
        else:
            hit = glob(pattern, name)
        if hit:
            return f"AllowedTools allows it ({pattern})"
    return None


def _json_schema(value: Any) -> Optional[dict[str, Any]]:
    """A gateway tool schema, written with CloudFormation's capitalized keys, as JSON Schema."""
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {}
    for key, item in value.items():
        name = key[:1].lower() + key[1:]
        if name == "properties" and isinstance(item, dict):
            out[name] = {k: _json_schema(v) or v for k, v in item.items()}
        elif name == "items":
            out[name] = _json_schema(item) or item
        else:
            out[name] = item
    return out
