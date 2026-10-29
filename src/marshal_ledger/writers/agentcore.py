"""Writer for Amazon Bedrock AgentCore: one ``AWS::BedrockAgentCore::Harness`` per CloudFormation template.

The harness is AgentCore's declarative agent: a model, a system prompt and tools, run by
AgentCore. It is what AWS recommends in place of Bedrock Agents, which no longer takes new
customers. The writer produces the harness, not its surrounding infrastructure: the
execution role and any gateways are referenced, not created. MCP servers with a URL,
gateways, the browser and the code interpreter are written as harness tools; any other tool
becomes an inline function, which returns control to the calling application, as the
CloudFormation writer's ``RETURN_CONTROL`` functions do for Bedrock Agents.

Every harness session also has the default ``shell`` and ``file_operations`` tools unless
``AllowedTools`` restricts them, so the writer lists the allowed tools whenever the source
doesn't have both.
"""

from __future__ import annotations

import re
from functools import partial
from typing import Any

from ..model import MARSHAL_NAMESPACE, AgentVersion
from ..readers.cloudformation import _HOSTED_TOOLS, FORMAT, HARNESS, HARNESS_BUILTINS
from ..resolve import _mcp_server
from .base import Writer, Written
from .cloudformation import _BEDROCK_MODEL, _deployable, _logical

_TOOL_NAME = re.compile(r"[^0-9a-zA-Z_-]+")
_PLATFORM = "bedrock-agentcore"
_PARAMETERS = {"temperature": "Temperature", "top_p": "TopP", "top_k": "TopK", "max_tokens": "MaxTokens"}
_MODEL_CONFIGS = {"openai": "OpenAiModelConfig", "azure-openai": "OpenAiModelConfig", "google": "GeminiModelConfig"}
_GATEWAY = "tool/bedrock-agentcore.gateway."
# Bedrock Agents settings and their place on AgentCore, from AWS's migration guide
# (docs.aws.amazon.com/bedrock/latest/userguide/agents-classic-maintenance-mode.html).
_CLASSIC = {
    "prompt_override": "a harness has one system prompt; Bedrock Agents' per-stage prompt overrides have no equivalent",
    "collaboration": "multi-agent collaboration needs agent-as-tool through a gateway, or custom code on AgentCore runtime",
    "external_collaborators": "multi-agent collaboration needs agent-as-tool through a gateway, or custom code on AgentCore runtime",
    "memory": "harness memory is an AgentCore Memory resource, enabled by default; Bedrock Agents' memory settings don't carry over",
    "idle_session_ttl": "harness sessions are managed by AgentCore; Bedrock Agents' idle session timeout doesn't apply",
    "orchestration": "the harness runs its own orchestration loop",
    "custom_orchestration": "custom orchestration runs as code on AgentCore runtime, not in a harness",
}
# Harness settings carried in the extension, written back as they were read.
_SETTINGS = {"memory": "Memory", "hooks": "Hooks", "skills": "Skills", "environment": "Environment",
             "environment_artifact": "EnvironmentArtifact", "truncation": "Truncation",
             "authorizer": "AuthorizerConfiguration", "max_iterations": "MaxIterations",
             "max_tokens": "MaxTokens", "timeout_seconds": "TimeoutSeconds"}


_HANDLED = {"resource_type", "model_config", "model_tools", "guardrail", "allowed_tools", "gateway_auth", "mcp_headers", "browsers",
            "code_interpreters", "gateways", "environment_variables", "endpoints", "conditional", *_SETTINGS}
_OTHER = {
    "managed_policies": "IAM managed policies are attached to the execution role, which this writer references but does not create",
    "artifact": "an AgentCore runtime's agent code; a harness declares its agent instead of running code",
    "protocol": "an AgentCore runtime setting; a harness is invoked through InvokeHarness",
    "network": "an AgentCore runtime setting; a harness's network is part of its environment",
    "lifecycle": "an AgentCore runtime setting; a harness's lifecycle is part of its environment",
}


def _harness_name(text: str) -> str:
    """A harness name: a letter, then up to 39 letters, digits and underscores."""
    name = re.sub(r"_+", "_", re.sub(r"[^0-9a-zA-Z_]+", "_", text)).strip("_") or "agent"
    return (name if name[0].isalpha() else f"a_{name}")[:40]


def _tool_name(text: str) -> str:
    return re.sub(r"([_-])[_-]+", r"\1", _TOOL_NAME.sub("_", text)).strip("_-")[:64] or "tool"


class AgentCoreHarnessWriter(Writer):
    format = "agentcore-harness"
    suffix = ".template.yaml"
    reader_format = FORMAT
    platform = _PLATFORM
    LOSSES = {
        "description": "a harness has no description; it is written as the template's description",
        "permissions.grants[*]": "IAM permissions live on the harness's execution role, which this writer references but does not create",
        "permissions.access[*]": "who may invoke the harness is governed by IAM policies or its inbound authorizer, outside the harness",
        "permissions.principal": "the execution role is a template parameter, set at deploy time",
        "permissions.identity": "the execution role is a template parameter, set at deploy time",
        "tools[*].scope": "harness tool definitions don't declare a scope",
        "tools[*].reversible": "harness tool definitions have no reversibility field",
        "tools[*].output_schema": "inline functions have no output schema",
        "tools[*].requires_confirmation": "harness tools have no confirmation setting: an inline function returns control to the "
                                          "application, which can ask a person, and a before_tool_call hook can require approval",
        "tools[agent/*]": "another agent is reached as a tool through an AgentCore gateway or MCP server, which this writer does not create",
        "knowledge[*]": "a harness reaches knowledge bases through an AgentCore gateway target or a retrieval tool, which this writer does not create",
        "knowledge[*].name": "knowledge sources are not written",
        "knowledge[*].type": "knowledge sources are not written",
        "knowledge[*].description": "knowledge sources are not written",
        "knowledge[*].scope": "knowledge sources are not written",
        "model.resolved": "the template names the requested model only",
        "model.provider": "the provider follows from the harness's model configuration",
    }
    MECHANICS = ("extensions.bedrock-agentcore.resource_type",)

    def write(self, record: AgentVersion) -> Written:
        spec = record.spec
        notes: dict[str, str] = {}
        warnings: list[str] = []
        ext: dict[str, Any] = dict(spec.extensions.get(_PLATFORM, {}))
        for platform, values in spec.extensions.items():
            if platform == "bedrock":
                for key in values:
                    if key != "guardrail":
                        notes[f"extensions.bedrock.{key}"] = _CLASSIC.get(key, "Bedrock Agents-specific; no harness equivalent")
            elif platform != _PLATFORM:
                notes[f"extensions.{platform}.*"] = f"{platform}-specific; no harness equivalent"

        parameters: dict[str, Any] = {}
        deployable = partial(_deployable, parameters=parameters, notes=notes)
        name = str(spec.display_name.value or record.agent.rsplit("/", 1)[-1])
        harness_name = _harness_name(name)
        if not spec.display_name.is_known:
            notes["display_name"] = "a harness requires a name; one was derived from the agent id"
        elif harness_name != name:
            notes["display_name"] = "harness names allow only letters, digits and underscores, up to 40 characters"
        props: dict[str, Any] = {"HarnessName": harness_name}

        principal = spec.permissions.principal.value if spec.permissions.principal.is_known else None
        if principal and str(principal).startswith("arn:aws:iam::"):
            props["ExecutionRoleArn"] = principal
        else:
            parameters["ExecutionRoleArn"] = {"Type": "String", "Description": "ARN of the IAM role the harness runs as"}
            props["ExecutionRoleArn"] = {"Ref": "ExecutionRoleArn"}

        props["Model"] = self._model(record, ext, parameters, notes, warnings, deployable)
        if spec.instructions.text:
            props["SystemPrompt"] = [{"Text": spec.instructions.text}]
        elif spec.instructions.hash:
            notes["instructions"] = "instruction text is not available (hash-only source)"

        tools, allowed, tool_refs = self._tools(record, ext, parameters, notes, deployable)
        # A tool ran in Lambda when the source names its executor, or when its scope came from the executor's role.
        executors = record.bindings.get("executors") or {}
        ran_in_lambda = {u.ref for u in record.spec.tools
                         if u.ref in executors or "executor Lambda" in (u.scope.basis or "")}
        executed = sorted(t["Name"] for t in tools if t["Type"] == "inline_function"
                          and tool_refs.get(t["Name"], f"tool/bedrock-agentcore.{record.agent.replace('/', '.')}.{t['Name']}") in ran_in_lambda)
        if executed:
            warnings.append(f"{', '.join(executed)} ran in Lambda in the source and {'is' if len(executed) == 1 else 'are'} "
                            "written as inline functions, which the calling application runs; to keep the Lambda, "
                            "put it behind an AgentCore gateway")
        if tools:
            props["Tools"] = tools
        if allowed is not None:
            props["AllowedTools"] = allowed

        for label, key in _SETTINGS.items():
            if label in ext:
                path = f"extensions.{_PLATFORM}.{label}"
                if _conditional(ext[label]):
                    notes[path] = "depends on a condition in the source template; set it before deploying"
                else:
                    props[key] = deployable(ext[label], path)
        if "environment_variables" in ext:
            notes[f"extensions.{_PLATFORM}.environment_variables"] = "environment variable values are not recorded; set them before deploying"
        if "gateways" in ext:
            notes[f"extensions.{_PLATFORM}.gateways"] = "the gateways are in the source stack, which this writer does not generate"
        for label in ("endpoints", "conditional"):
            if label in ext:
                notes[f"extensions.{_PLATFORM}.{label}"] = "describes the surrounding stack, which this writer does not generate"
        for label in set(ext) - _HANDLED:
            notes[f"extensions.{_PLATFORM}.{label}"] = _OTHER.get(label, "not part of a harness definition")

        metadata: dict[str, Any] = {"agent": record.agent}
        if record.platform != _PLATFORM:
            metadata["platform"] = record.platform
        if tool_refs:
            metadata["tool_refs"] = tool_refs
        resource = {"Type": HARNESS, "Metadata": {MARSHAL_NAMESPACE: metadata}, "Properties": props}
        template: dict[str, Any] = {"AWSTemplateFormatVersion": "2010-09-09"}
        if spec.description.is_known:
            template["Description"] = str(spec.description.value)
        if parameters:
            template["Parameters"] = parameters
        template["Resources"] = {_logical(record.agent.rsplit("/", 1)[-1]): resource}
        return Written(document=template, notes=notes, warnings=warnings)

    def _model(self, record: AgentVersion, ext: dict[str, Any], parameters: dict[str, Any], notes: dict[str, str],
               warnings: list[str], deployable: Any) -> dict[str, Any]:
        spec = record.spec
        written: dict[str, Any] = dict(ext.get("model_config") or {})
        kind = written.pop("type", None)
        provider = str(spec.model.provider.value) if spec.model.provider.is_known else ""
        if spec.model.requested.is_known:
            model_id: Any = spec.model.requested.value
            kind = kind or (_MODEL_CONFIGS.get(provider) if not _BEDROCK_MODEL.search(str(model_id)) else None) or "BedrockModelConfig"
            if kind == "BedrockModelConfig" and not _BEDROCK_MODEL.search(str(model_id)):
                warnings.append(f"{model_id} is not a Bedrock model id; choose one before deploying")
        else:
            kind = kind or "BedrockModelConfig"
            parameters["ModelId"] = {"Type": "String", "Description": "Model the harness uses"}
            model_id = {"Ref": "ModelId"}
            warnings.append("the source names no model; set ModelId before deploying")
        if kind != "BedrockModelConfig" and "model_config" not in ext:
            notes[f"extensions.{_PLATFORM}.model_config"] = f"the model's provider selects the harness's {kind}"
        config: dict[str, Any] = {"ModelId": model_id}
        if kind in ("OpenAiModelConfig", "GeminiModelConfig"):
            parameters["ModelApiKeyArn"] = {"Type": "String", "Description": "ARN of the credential provider that holds the model API key"}
            config["ApiKeyArn"] = {"Ref": "ModelApiKeyArn"}
        for name, value in spec.model.parameters.items():
            key = _PARAMETERS.get(name)
            if key and not (key == "TopK" and kind != "GeminiModelConfig"):
                config[key] = value
            else:
                notes[f"model.parameters.{name}"] = "a harness model takes temperature, top_p and max_tokens (and top_k for Gemini)"
        path = f"extensions.{_PLATFORM}.model_config"
        for key, value in written.items():
            if _conditional(value):
                notes[path] = "part of it depends on a condition in the source template; set it before deploying"
            else:
                config[key] = deployable(value, path)
        if ext.get("model_tools"):
            params = dict(config.get("AdditionalParams") or {})
            params["tools"] = deployable(ext["model_tools"], f"extensions.{_PLATFORM}.model_tools")
            config["AdditionalParams"] = params
        guardrail = ext.get("guardrail") or _classic_guardrail(spec.extensions.get("bedrock", {}).get("guardrail"))
        if guardrail:
            # The Converse API's guardrailConfig, applied by Bedrock to every model call.
            params = dict(config.get("AdditionalParams") or {})
            params["guardrailConfig"] = deployable(guardrail, f"extensions.{_PLATFORM}.guardrail")
            config["AdditionalParams"] = params
            if "guardrail" in spec.extensions.get("bedrock", {}):
                notes["extensions.bedrock.guardrail"] = "written as the model's guardrail configuration (extensions.bedrock-agentcore.guardrail)"
                notes[f"extensions.{_PLATFORM}.guardrail"] = "the Bedrock agent's guardrail, applied to every model call"
        return {kind: config}

    def _tools(self, record: AgentVersion, ext: dict[str, Any], parameters: dict[str, Any], notes: dict[str, str],
               deployable: Any) -> tuple[list[dict[str, Any]], Any, dict[str, str]]:
        spec = record.spec
        local = f"tool/bedrock-agentcore.{record.agent.replace('/', '.')}"
        tools: list[dict[str, Any]] = []
        allowed: list[str] = []
        builtins: set[str] = set()
        tool_refs: dict[str, str] = {}
        used: set[str] = set()
        written_gateways: set[str] = set()

        def unique(text: str) -> str:
            name = _tool_name(text)
            while name in used:
                name += "_"
            used.add(name)
            return name

        def add(entry: dict[str, Any], pattern: str, ref: str, derived: str) -> None:
            tools.append(entry)
            allowed.append(pattern)
            if ref != derived:
                tool_refs[entry["Name"]] = ref

        model_tools = {_HOSTED_TOOLS[t["type"]][0] for t in ext.get("model_tools") or []
                       if isinstance(t, dict) and t.get("type") in _HOSTED_TOOLS}
        for use in spec.tools:
            base = f"tools[{use.ref}]"
            if use.ref in model_tools:
                continue  # written back with the model's settings
            builtin = use.ref.removeprefix("tool/bedrock-agentcore.")
            if builtin in HARNESS_BUILTINS:
                builtins.add(builtin)
                continue
            if use.ref.startswith("agent/"):
                continue
            if use.ref == "tool/bedrock.user_input":
                notes[f"{base}*"] = "a harness has no built-in tool for asking the user; the model asks in its reply"
                continue
            if use.ref.startswith(_GATEWAY):
                in_stack = next((n for n, b in (ext.get("gateways") or {}).items() if use.ref == b or use.ref.startswith(b + ".")), None)
                if in_stack is not None:
                    notes[f"{base}*"] = (f"reached through the AgentCore gateway {in_stack}, defined in the source stack; "
                                         "the harness references it by a template parameter")
                    if in_stack in written_gateways:
                        continue
                    written_gateways.add(in_stack)
                    parameter = f"{_logical(in_stack)}GatewayArn"
                    parameters.setdefault(parameter, {"Type": "String", "Description": f"ARN of the AgentCore gateway {in_stack}"})
                    arn: Any = {"Ref": parameter}
                    name = unique(in_stack)
                    notes[f"tools[{local}.{name}]*"] = (f"stands for the tools of the AgentCore gateway {in_stack}, "
                                                        "which the written template references by a parameter")
                else:
                    rest = use.ref.removeprefix(_GATEWAY)
                    arn = {"Fn::ImportValue": rest.removeprefix("import:")} if rest.startswith("import:") else rest
                    name = unique(use.name or "gateway")
                entry = {"Type": "agentcore_gateway", "Name": name, "Config": {"AgentCoreGateway": {"GatewayArn": arn}}}
                auth = (ext.get("gateway_auth") or {}).get(in_stack or use.name or "")
                if auth is not None:
                    entry["Config"]["AgentCoreGateway"]["OutboundAuth"] = deployable(auth, f"extensions.{_PLATFORM}.gateway_auth")
                add(entry, f"@{name}", use.ref, use.ref)
                continue
            if use.ref.startswith("tool/mcp."):
                server = _mcp_server(use.ref, use.name)
                if server.startswith(("https://", "http://")):
                    name = unique(use.name or re.sub(r"^https?://", "", server))
                    add({"Type": "remote_mcp", "Name": name, "Config": {"RemoteMcp": {"Url": server}}},
                        f"@{name}", use.ref, f"tool/mcp.{server}")
                    if (ext.get("mcp_headers") or {}).get(use.name or ""):
                        notes[f"extensions.{_PLATFORM}.mcp_headers"] = "MCP header values are not recorded; set them before deploying"
                    continue
                notes[f"{base}*"] = ("a harness connects to MCP servers by URL; put this server behind an AgentCore gateway "
                                     "or run it on AgentCore runtime")
                continue
            if use.ref.endswith("code_interpreter") or use.ref == "tool/google.code_execution":
                name = unique(use.name or "code_interpreter")
                entry: dict[str, Any] = {"Type": "agentcore_code_interpreter", "Name": name}
                arn = (ext.get("code_interpreters") or {}).get(use.name or "")
                if arn is not None:
                    entry["Config"] = {"AgentCoreCodeInterpreter": {"CodeInterpreterArn": deployable(arn, f"extensions.{_PLATFORM}.code_interpreters")}}
                add(entry, name, use.ref, "tool/bedrock-agentcore.code_interpreter")
                continue
            if use.ref == "tool/bedrock-agentcore.browser":
                name = unique(use.name or "browser")
                entry = {"Type": "agentcore_browser", "Name": name}
                arn = (ext.get("browsers") or {}).get(use.name or "")
                if arn is not None:
                    entry["Config"] = {"AgentCoreBrowser": {"BrowserArn": deployable(arn, f"extensions.{_PLATFORM}.browsers")}}
                add(entry, name, use.ref, use.ref)
                continue
            # Anything else returns control to the application, which runs the tool.
            name = unique(use.name or use.ref.rsplit(".", 1)[-1])
            if name != use.name:
                notes[f"{base}.name"] = "harness tool names allow only letters, digits, hyphens and underscores"
            description = use.description
            if not description:
                description = f"Runs {use.name or use.ref} in the calling application"
                notes[f"{base}.description"] = "an inline function requires a description; one was generated"
            schema = use.input_schema if isinstance(use.input_schema, dict) else None
            if schema is None:
                schema = {"type": "object", "properties": {}}
                notes[f"{base}.input_schema"] = "an inline function requires an input schema; an empty one was written"
            add({"Type": "inline_function", "Name": name,
                 "Config": {"InlineFunction": {"Description": description, "InputSchema": schema}}},
                name, use.ref, f"{local}.{name}")
            if use.requires_confirmation.value is True:
                notes[f"{base}.requires_confirmation"] = (
                    "the source requires a person to confirm each call; the inline function returns control to the "
                    "application, which must ask, or add a before_tool_call hook that does")

        if "allowed_tools" in ext:
            written_allowed: Any = deployable(ext["allowed_tools"], f"extensions.{_PLATFORM}.allowed_tools")
        elif builtins == set(HARNESS_BUILTINS):
            written_allowed = None  # every tool, as when AllowedTools is not set
        else:
            # Without a list, every session would also get the default shell and file tools.
            written_allowed = sorted(builtins) + allowed
            notes[f"extensions.{_PLATFORM}.allowed_tools"] = (
                "written so the harness gets the default shell and file tools only where the source has them")
        return tools, written_allowed, tool_refs


def _conditional(value: Any) -> bool:
    """Whether part of ``value`` depended on a template condition (read as ``${Fn::If}``), which
    can't be written back as a parameter where the template expects an object."""
    if isinstance(value, dict):
        return any(_conditional(v) for v in value.values())
    if isinstance(value, list):
        return any(_conditional(v) for v in value)
    return isinstance(value, str) and re.fullmatch(r"\$\{(Fn::\w+|Condition)\}", value) is not None


def _classic_guardrail(value: Any) -> Any:
    """A Bedrock Agents GuardrailConfiguration as the Converse API's guardrailConfig."""
    if not isinstance(value, dict) or "GuardrailIdentifier" not in value:
        return None
    out = {"guardrailIdentifier": value["GuardrailIdentifier"]}
    if "GuardrailVersion" in value:
        out["guardrailVersion"] = value["GuardrailVersion"]
    return out
