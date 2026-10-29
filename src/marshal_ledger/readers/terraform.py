"""Reader for Terraform plans and state: Bedrock Agents, AgentCore runtimes and harnesses.

Reads the JSON that ``terraform show -json`` prints for a saved plan (what a pipeline is
about to apply) or for the current state. Marshal never runs Terraform: the pipeline that
already plans and applies produces the file.

Two providers define Bedrock agents. The AWS provider (``aws_bedrockagent_agent`` with
separate action group, knowledge base association, collaborator and alias resources)
and the Cloud Control provider (``awscc_bedrock_agent``, shaped like CloudFormation).
Both are translated into the CloudFormation form of the same resources, together with
the IAM roles, policies and Lambda functions they refer to, and read by the
CloudFormation reader's Bedrock mapping, so an agent maps the same way whichever tool
defines it. AgentCore runtimes come from either provider, and AgentCore harnesses from
the Cloud Control provider (``awscc_bedrockagentcore_harness``).

A value Terraform knows only after apply is followed through the plan's configuration
to the resource it refers to, across module inputs and outputs, the way CloudFormation's
``GetAtt`` is. Anything else unknown stays ``unknown``. Only allowlisted attributes are
read, and any value Terraform marks as sensitive is left out.

Mapping rules are documented in ``docs/formats/terraform.md``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Iterator, Optional

from ..model import MARSHAL_NAMESPACE
from .base import DocumentReader, ReadOptions, ReadResult
from .cloudformation import (
    AGENT,
    ALIAS,
    FUNCTION,
    KNOWLEDGE_BASE,
    POLICY,
    ROLE,
    RUNTIME,
    RUNTIME_ENDPOINT,
    HARNESS,
    CloudFormationReader,
    _Template,
)

FORMAT = "terraform"
AGENT_TYPES = {"aws_bedrockagent_agent", "awscc_bedrock_agent", "aws_bedrockagentcore_agent_runtime",
               "awscc_bedrockagentcore_runtime", "awscc_bedrockagentcore_harness"}
UNKNOWN = "${known after apply}"  # a nested reference no expression explains
SENSITIVE = "${sensitive}"

# Blocks the AWS provider writes as one-item lists, where CloudFormation has an object.
_SINGLE = {
    "guardrail_configuration", "memory_configuration", "prompt_override_configuration", "inference_configuration",
    "action_group_executor", "api_schema", "s3", "function_schema", "agent_descriptor", "session_summary_configuration",
    "custom_orchestration", "executor", "lifecycle_configuration", "network_configuration", "network_mode_config",
    "authorizer_configuration", "custom_jwt_authorizer", "agent_runtime_artifact", "container_configuration",
    "code_configuration", "code", "request_header_configuration", "knowledge_base_configuration",
}
# Maps whose keys are names the user chose, kept as written.
_MAPS = {"parameters", "environment_variables", "tags", "additional_model_request_fields", "headers", "custom_parameters",
         "retrieval_config"}
# Property names whose CloudFormation spelling isn't plain PascalCase.
_SPELLING = {"idle_session_ttl_in_seconds": "IdleSessionTTLInSeconds", "custom_jwt_authorizer": "CustomJWTAuthorizer"}

_AGENT_ATTRIBUTES = [
    "agent_name", "foundation_model", "instruction", "description", "agent_resource_role_arn",
    "idle_session_ttl_in_seconds", "customer_encryption_key_arn", "prompt_override_configuration",
    "guardrail_configuration", "memory_configuration", "agent_collaboration", "orchestration_type",
    "custom_orchestration", "action_groups", "knowledge_bases", "agent_collaborators",
]
_RUNTIME_ATTRIBUTES = [
    "agent_runtime_name", "description", "role_arn", "agent_runtime_artifact", "network_configuration",
    "authorizer_configuration", "lifecycle_configuration", "environment_variables", "protocol_configuration",
]
_HARNESS_ATTRIBUTES = [
    "harness_name", "model", "system_prompt", "tools", "allowed_tools", "execution_role_arn", "memory", "skills",
    "environment", "environment_artifact", "environment_variables", "truncation", "authorizer_configuration",
    "max_iterations", "max_tokens", "timeout_seconds",
]
# Attributes that name a resource, so a known value can be matched to the resource it names.
_IDENTIFIERS = ("arn", "id", "name", "agent_id", "agent_arn", "agent_alias_arn", "agent_alias_id", "function_name",
                "knowledge_base_id", "knowledge_base_arn", "agent_runtime_id", "agent_runtime_arn", "role_name")
# Attributes whose value names another resource. Only these are linked back to a resource
# in the plan when the value is already known, as in state JSON.
_LINKED = {"agent_resource_role_arn", "role", "role_arn", "role_name", "lambda", "knowledge_base_id", "alias_arn",
           "agent_id", "agent_runtime_id", "policy_arn", "execution_role_arn"}
# What is left out when an attribute's value comes through a local, by attribute.
_LOCAL_CONSEQUENCES = {
    "role": "so this policy's permissions aren't linked to a role",
    "role_name": "so this policy's permissions aren't linked to a role",
    "agent_resource_role_arn": "so the agent's role, and its permissions, aren't linked",
    "role_arn": "so the role, and its permissions, aren't linked",
    "execution_role_arn": "so the role, and its permissions, aren't linked",
    "alias_arn": "so this collaborator isn't linked to its agent",
    "agent_id": "so this isn't linked to its agent",
    "agent_runtime_id": "so this isn't linked to its runtime",
    "lambda": "so the Lambda function that runs this action group isn't linked",
    "knowledge_base_id": "so this knowledge base isn't linked",
    "action_groups": "so if the agent has action groups, they aren't read",
    "knowledge_bases": "so if the agent has knowledge bases, they aren't read",
    "agent_collaborators": "so if the agent has collaborators, they aren't read",
}

# Where a CloudFormation property's Terraform attribute isn't its snake_case name.
_ATTRIBUTE_NAMES = {("aws_iam_role", "RoleName"): "name", ("aws_bedrockagent_agent", "IdleSessionTTLInSeconds"): "idle_session_ttl_in_seconds",
                    ("awscc_bedrock_agent", "IdleSessionTTLInSeconds"): "idle_session_ttl_in_seconds"}
_REFERENCE = re.compile(r"^([a-z0-9_]+)\.([A-Za-z0-9_-]+)(\[[^\]]+\])?(?:\.([A-Za-z0-9_]+))?")


def pascal(name: str) -> str:
    return _SPELLING.get(name) or "".join(part[:1].upper() + part[1:] for part in name.split("_"))


def _snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()


@dataclass
class _Instance:
    address: str
    module: tuple[str, ...]
    type: str
    name: str
    index: Any
    values: dict[str, Any]
    unknown: Any = field(default_factory=dict)
    sensitive: Any = field(default_factory=dict)
    expressions: dict[str, Any] = field(default_factory=dict)

    @property
    def config_address(self) -> str:
        return f"{self.type}.{self.name}"


class _Plan:
    """A plan or state document: its resource instances, and the configuration that links them."""

    def __init__(self, data: dict[str, Any]):
        self.data = data
        root = (data.get("planned_values") or data.get("values") or {}).get("root_module") or {}
        changes = {c.get("address"): c.get("change") or {} for c in data.get("resource_changes") or []}
        self.instances: dict[str, _Instance] = {}
        self.modules: dict[tuple[str, ...], dict[str, Any]] = {}
        self._index_config(((data.get("configuration") or {}).get("root_module")) or {}, ())
        for resource in self._resources(root):
            if resource.get("mode") != "managed":
                continue
            address = str(resource.get("address"))
            module = tuple(re.findall(r"module\.([A-Za-z0-9_-]+)(?:\[[^\]]*\])?\.", address))
            change = changes.get(address) or {}
            config = self._config(module, f"{resource.get('type')}.{resource.get('name')}")
            self.instances[address] = _Instance(
                address=address, module=module, type=str(resource.get("type")), name=str(resource.get("name")),
                index=resource.get("index"), values=resource.get("values") or {},
                unknown=change.get("after_unknown") or {},
                sensitive=resource.get("sensitive_values") or change.get("after_sensitive") or {},
                expressions=(config or {}).get("expressions") or {},
            )

    @staticmethod
    def _resources(module: dict[str, Any]) -> Iterator[dict[str, Any]]:
        yield from module.get("resources") or []
        for child in module.get("child_modules") or []:
            yield from _Plan._resources(child)

    def _index_config(self, module: dict[str, Any], path: tuple[str, ...]) -> None:
        self.modules[path] = module
        for name, call in (module.get("module_calls") or {}).items():
            self._index_config(call.get("module") or {}, path + (name,))

    def _config(self, module: tuple[str, ...], address: str) -> Optional[dict[str, Any]]:
        return next((r for r in (self.modules.get(module) or {}).get("resources") or []
                     if r.get("address") == address), None)

    def of_type(self, *types: str) -> list[_Instance]:
        return [i for i in self.instances.values() if i.type in types]

    def resolve(self, module: tuple[str, ...], references: list[str], depth: int = 0,
                locals_seen: Optional[list[str]] = None) -> Optional[tuple[_Instance, str]]:
        """The resource instance, and attribute, that the first resolvable reference names.

        Module inputs (``var.x``) are followed to the calling module's argument, and module
        outputs (``module.m.x``) into the called module's output expression. Locals a reference
        passes through, which a plan doesn't record, are added to ``locals_seen``.
        """
        if depth > 20:
            return None
        for reference in references:
            if reference.startswith("var."):
                if not module:
                    continue
                call = ((self.modules.get(module[:-1]) or {}).get("module_calls") or {}).get(module[-1]) or {}
                argument = (call.get("expressions") or {}).get(reference.split(".")[1])
                found = self.resolve(module[:-1], (argument or {}).get("references") or [], depth + 1, locals_seen) \
                    if isinstance(argument, dict) else None
            elif reference.startswith("module."):
                parts = reference.split(".")
                if len(parts) < 3:
                    continue
                child = module + (re.sub(r"\[.*\]$", "", parts[1]),)
                output = (((self.modules.get(child) or {}).get("outputs") or {}).get(parts[2]) or {}).get("expression") or {}
                found = self.resolve(child, output.get("references") or [], depth + 1, locals_seen)
            elif reference.split(".", 1)[0] in ("data", "local", "path", "each", "count", "self", "terraform"):
                if reference.startswith("local.") and locals_seen is not None:
                    name = "local." + reference.split(".")[1].split("[")[0]
                    if name not in locals_seen:
                        locals_seen.append(name)
                continue
            else:
                found = self._instance(module, reference)
            if found:
                return found
        return None

    def _instance(self, module: tuple[str, ...], reference: str) -> Optional[tuple[_Instance, str]]:
        match = _REFERENCE.match(reference)
        if not match:
            return None
        kind, name, index, attribute = match.groups()
        candidates = [i for i in self.instances.values() if i.module == module and i.type == kind and i.name == name]
        if index:
            key = json.loads(index[1:-1]) if index[1:-1].startswith('"') else index[1:-1]
            candidates = [i for i in candidates if str(i.index) == str(key)]
        if len(candidates) != 1:
            return None
        return candidates[0], attribute or "id"


class _TerraformTemplate(_Template):
    """A plan translated into the CloudFormation resources it defines, keyed by Terraform address."""

    source_format = FORMAT

    def __init__(self, plan: _Plan, artifact: str, folder: str):
        self.plan = plan
        self.types: dict[str, str] = {}
        self._translated: dict[str, dict[str, Any]] = {}
        self._known: dict[str, str] = {}
        self.warnings: list[str] = []
        for instance in plan.instances.values():
            for attribute in _IDENTIFIERS:
                value = instance.values.get(attribute)
                if isinstance(value, str) and value:
                    self._known.setdefault(value, instance.address)
        resources = self._translate(folder)
        super().__init__({"Resources": resources}, artifact)

    def resource_type(self, logical: str) -> str:
        return self.types.get(logical, super().resource_type(logical))

    def path(self, logical: str, prop: str = "") -> str:
        if not prop:
            return logical
        head, bracket, rest = prop.partition("[")
        attribute = _ATTRIBUTE_NAMES.get((self.types.get(logical), head)) or _snake(head)
        return f"{logical}.{attribute}" + (bracket + rest if bracket else "")

    # -- values ------------------------------------------------------------

    def _value(self, instance: _Instance, attribute: str) -> Any:
        """An attribute's value: known values as planned, unknown ones as references or tokens.
        Sensitive values are never copied."""
        return self._merge(instance, attribute, instance.values.get(attribute), _at(instance.unknown, attribute),
                           _at(instance.sensitive, attribute), instance.expressions.get(attribute))

    def _merge(self, instance: _Instance, name: str, value: Any, unknown: Any, sensitive: Any, expression: Any,
               inherited: bool = False) -> Any:
        if sensitive is True:
            return SENSITIVE
        if unknown is True:
            # Known only after apply. Follow it to the resource it comes from; a value that comes from
            # no resource is an optional attribute left unset (or one routed through a local, which a
            # plan doesn't record), so it is treated as absent rather than guessed.
            if inherited:
                # One expression covers the whole object, so which reference fills this field is unknown,
                # unless no reference in it can be: then the provider computes the field, as for an optional
                # attribute the configuration leaves unset (the Cloud Control provider marks those unknown).
                references = expression.get("references") if isinstance(expression, dict) else None
                seen: list[str] = []
                if references and (any(r.startswith("data.") for r in references)
                                   or self.plan.resolve(instance.module, references, locals_seen=seen) or seen):
                    return UNKNOWN
                return None
            references = expression.get("references") if isinstance(expression, dict) else None
            locals_seen: list[str] = []
            found = self.plan.resolve(instance.module, references, locals_seen=locals_seen) if references else None
            if found:
                return {"Fn::GetAtt": [found[0].address, pascal(found[1])]}
            if locals_seen:
                self._warn_local(instance, name, locals_seen)
            return UNKNOWN if expression is None and name in _LINKED else None
        if isinstance(value, dict):
            # A plan leaves out keys known only after apply, and lists them in after_unknown.
            keys = list(value) + [k for k in (unknown if isinstance(unknown, dict) else {}) if k not in value]
            # Blocks have an expression per field; an object attribute has one for the whole value.
            whole = inherited or (isinstance(expression, dict) and ("references" in expression or "constant_value" in expression))
            merged = {k: self._merge(instance, k, value.get(k), _at(unknown, k), _at(sensitive, k),
                                     expression if whole else _at(expression, k), whole)
                      for k in keys}
            return {k: v for k, v in merged.items() if v is not None}
        if isinstance(value, list):
            whole = inherited or (isinstance(expression, dict) and ("references" in expression or "constant_value" in expression))
            return [self._merge(instance, name, v, _item(unknown, i), _item(sensitive, i),
                                expression if whole else _item(expression, i), whole)
                    for i, v in enumerate(value)]
        if name in _LINKED and isinstance(value, str) and value in self._known and self._known[value] != instance.address:
            # A literal ARN or id of another resource in the plan, as state JSON gives it: link to it.
            return {"Fn::GetAtt": [self._known[value], "Arn"]}
        return value

    def _warn_local(self, instance: _Instance, name: str, locals_seen: list[str]) -> None:
        """Say when a value isn't followed because it comes through a local, and what that leaves out,
        so a missing link isn't read as a missing permission or tool."""
        consequence = _LOCAL_CONSEQUENCES.get(name, "so if it is set, it is recorded as unknown")
        message = (f"{instance.address}: {name} comes from {', '.join(locals_seen)}, which a Terraform plan "
                   f"doesn't record, {consequence}")
        if message not in self.warnings:
            self.warnings.append(message)

    def _props(self, instance: _Instance, attributes: list[str]) -> dict[str, Any]:
        props: dict[str, Any] = {}
        for attribute in attributes:
            value = self._value(instance, attribute)
            if value is None or value == [] or value == {}:
                continue
            props[pascal(attribute)] = _cloudformation(attribute, value)
        return props

    def _ref(self, instance: _Instance, attribute: str) -> Optional[str]:
        """The address of the resource an attribute refers to, if it is in the plan."""
        value = self._value(instance, attribute)
        if isinstance(value, dict) and "Fn::GetAtt" in value:
            return value["Fn::GetAtt"][0]
        return None

    # -- resources ---------------------------------------------------------

    def _add(self, instance: _Instance, kind: str, props: dict[str, Any], metadata: Optional[dict[str, Any]] = None) -> None:
        resource: dict[str, Any] = {"Type": kind, "Properties": props}
        if metadata:
            resource["Metadata"] = {MARSHAL_NAMESPACE: metadata}
        self._translated[instance.address] = resource
        self.types[instance.address] = instance.type

    def _translate(self, folder: str) -> dict[str, dict[str, Any]]:
        plan = self.plan
        agent_id = (lambda address: f"{folder}/{address}" if folder else address)
        for role in plan.of_type("aws_iam_role", "awscc_iam_role"):
            props = self._props(role, ["role_name"]) if role.type == "awscc_iam_role" else {}
            if role.type == "aws_iam_role":
                name = self._value(role, "name")
                if name is not None:
                    props["RoleName"] = name
                props["Policies"] = [
                    {"PolicyName": p.get("name"), "PolicyDocument": _document(p.get("policy"))}
                    for p in self._value(role, "inline_policy") or [] if isinstance(p, dict) and _document(p.get("policy"))
                ]
                props["ManagedPolicyArns"] = [a for a in self._value(role, "managed_policy_arns") or [] if isinstance(a, str)]
            else:
                props["Policies"] = [
                    {"PolicyName": p.get("policy_name"), "PolicyDocument": _document(p.get("policy_document"))}
                    for p in self._value(role, "policies") or [] if isinstance(p, dict) and _document(p.get("policy_document"))
                ]
                props["ManagedPolicyArns"] = [a for a in self._value(role, "managed_policy_arns") or [] if isinstance(a, str)]
            self._add(role, ROLE, {k: v for k, v in props.items() if v not in (None, [])})
        for policy in plan.of_type("aws_iam_role_policy", "awscc_iam_role_policy"):
            role = self._ref(policy, "role" if policy.type == "aws_iam_role_policy" else "role_name")
            document = _document(self._value(policy, "policy" if policy.type == "aws_iam_role_policy" else "policy_document"))
            if role and document:
                self._add(policy, POLICY, {"Roles": [{"Ref": role}], "PolicyDocument": document})
        for attachment in plan.of_type("aws_iam_role_policy_attachment"):
            role = self._ref(attachment, "role")
            if not role or role not in self._translated:
                continue
            managed = self._ref(attachment, "policy_arn")
            document = _document(self._value(plan.instances[managed], "policy")) if managed else None
            if document:  # a customer managed policy in the same plan: read its statements
                self._add(attachment, POLICY, {"Roles": [{"Ref": role}], "PolicyDocument": document})
            elif isinstance(arn := self._value(attachment, "policy_arn"), str):
                self._translated[role]["Properties"].setdefault("ManagedPolicyArns", []).append(arn)
        for function in plan.of_type("aws_lambda_function", "awscc_lambda_function"):
            self._add(function, FUNCTION, {k: v for k, v in {
                "FunctionName": self._value(function, "function_name"), "Role": self._value(function, "role"),
            }.items() if v is not None})
        for kb in plan.of_type("aws_bedrockagent_knowledge_base", "awscc_bedrock_knowledge_base"):
            self._add(kb, KNOWLEDGE_BASE, self._props(kb, ["name", "description", "knowledge_base_configuration"]))
        for alias in plan.of_type("aws_bedrockagent_agent_alias", "awscc_bedrock_agent_alias"):
            props = self._props(alias, ["agent_alias_name"])
            if target := self._ref(alias, "agent_id"):
                props["AgentId"] = {"Fn::GetAtt": [target, "AgentId"]}
            self._add(alias, ALIAS, props)

        for agent in plan.of_type("aws_bedrockagent_agent", "awscc_bedrock_agent"):
            props = self._props(agent, _AGENT_ATTRIBUTES)
            if agent.type == "aws_bedrockagent_agent":
                props.update(self._attached(agent))
            self._add(agent, AGENT, props, {"agent": agent_id(agent.address)})

        for runtime in plan.of_type("aws_bedrockagentcore_agent_runtime", "awscc_bedrockagentcore_runtime"):
            props = self._props(runtime, _RUNTIME_ATTRIBUTES)
            protocol = props.get("ProtocolConfiguration")
            if isinstance(protocol, dict):  # the AWS provider nests what CloudFormation gives as a string
                props["ProtocolConfiguration"] = protocol.get("ServerProtocol")
            self._add(runtime, RUNTIME, props, {"agent": agent_id(runtime.address)})
        for endpoint in plan.of_type("aws_bedrockagentcore_agent_runtime_endpoint", "awscc_bedrockagentcore_runtime_endpoint"):
            props = self._props(endpoint, ["name"])
            if target := self._ref(endpoint, "agent_runtime_id"):
                props["AgentRuntimeId"] = {"Ref": target}
            self._add(endpoint, RUNTIME_ENDPOINT, props)

        for harness in plan.of_type("awscc_bedrockagentcore_harness"):
            props = self._props(harness, _HARNESS_ATTRIBUTES)
            for tool in props.get("Tools") or []:
                function = ((tool.get("Config") or {}).get("InlineFunction") or {}) if isinstance(tool, dict) else {}
                if isinstance(function.get("InputSchema"), str):  # the Cloud Control provider gives it as JSON text
                    try:
                        function["InputSchema"] = json.loads(function["InputSchema"])
                    except ValueError:
                        pass
            self._add(harness, HARNESS, props, {"agent": agent_id(harness.address)})
        return self._translated

    def _attached(self, agent: _Instance) -> dict[str, Any]:
        """The AWS provider's separate action group, knowledge base and collaborator resources for an agent."""
        plan = self.plan
        props: dict[str, Any] = {}
        groups = []
        for group in plan.of_type("aws_bedrockagent_agent_action_group"):
            if self._ref(group, "agent_id") != agent.address:
                continue
            item = self._props(group, ["action_group_name", "description", "action_group_state",
                                       "parent_action_group_signature", "action_group_executor", "api_schema"])
            functions = ((self._value(group, "function_schema") or [{}])[0] or {}).get("member_functions") or []
            members = [f for block in functions if isinstance(block, dict) for f in block.get("functions") or []]
            if members:
                item["FunctionSchema"] = {"Functions": [_function(f) for f in members if isinstance(f, dict)]}
            groups.append(item)
        if groups:
            props["ActionGroups"] = sorted(groups, key=lambda g: str(g.get("ActionGroupName")))
        bases = []
        for association in plan.of_type("aws_bedrockagent_agent_knowledge_base_association"):
            if self._ref(association, "agent_id") == agent.address:
                bases.append(self._props(association, ["knowledge_base_id", "description", "knowledge_base_state"]))
        if bases:
            props["KnowledgeBases"] = bases
        collaborators = []
        for collaborator in plan.of_type("aws_bedrockagent_agent_collaborator"):
            if self._ref(collaborator, "agent_id") == agent.address:
                item = self._props(collaborator, ["collaborator_name", "collaboration_instruction",
                                                  "relay_conversation_history", "agent_descriptor"])
                collaborators.append(item)
        if collaborators:
            props["AgentCollaborators"] = collaborators
        return props


def _at(node: Any, key: str) -> Any:
    return node.get(key) if isinstance(node, dict) else node if isinstance(node, bool) else None


def _item(node: Any, index: int) -> Any:
    if isinstance(node, list):
        return node[index] if index < len(node) else None
    return node if isinstance(node, bool) else None


def _document(value: Any) -> Optional[dict[str, Any]]:
    """An IAM policy document from its JSON text, or None when it isn't known at plan time."""
    if isinstance(value, str) and value.startswith("{"):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) and "Statement" in value else None


def _cloudformation(name: str, value: Any) -> Any:
    """A Terraform attribute value in CloudFormation's shape: PascalCase keys, one-item block lists as objects."""
    if name in _SINGLE and isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        if "Fn::GetAtt" in value or "Ref" in value:
            return value
        if name in _MAPS:
            return {k: _cloudformation("", v) for k, v in value.items()}
        return {pascal(k): _cloudformation(k, v) for k, v in value.items() if v is not None and v != []}
    if isinstance(value, list):
        return [_cloudformation(name, v) for v in value]
    return value


def _function(function: dict[str, Any]) -> dict[str, Any]:
    """An AWS provider function definition, whose parameters are a set of blocks keyed by ``map_block_key``."""
    out = {pascal(k): v for k, v in function.items() if k in ("name", "description", "require_confirmation") and v is not None}
    parameters = {}
    for parameter in function.get("parameters") or []:
        if isinstance(parameter, dict) and parameter.get("map_block_key"):
            parameters[parameter["map_block_key"]] = {
                pascal(k): v for k, v in parameter.items() if k in ("type", "description", "required") and v is not None
            }
    if parameters:
        out["Parameters"] = parameters
    return out


class TerraformReader(DocumentReader):
    format = FORMAT
    platform = "bedrock"

    @classmethod
    def matches(cls, data: Any) -> bool:
        if not isinstance(data, dict) or "terraform_version" not in data:
            return False
        root = (data.get("planned_values") or data.get("values") or {}).get("root_module") or {}
        return any(r.get("type") in AGENT_TYPES for r in _Plan._resources(root))

    def read_data(self, data: Any, artifact: str, options: ReadOptions | None = None) -> ReadResult:
        folder = str(PurePosixPath(artifact).parent)
        template = _TerraformTemplate(_Plan(data), artifact, "" if folder == "." else folder)
        result = CloudFormationReader().read_template(template, options)
        result.warnings += [f"{artifact}: {w}" for w in template.warnings]
        return result


__all__ = ["FORMAT", "TerraformReader", "pascal"]
