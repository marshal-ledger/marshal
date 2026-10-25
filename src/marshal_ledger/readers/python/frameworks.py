"""Framework profiles: how each agent framework's constructors map to the ledger model.

Each profile recognizes its agent constructors by qualified name and maps their
statically known arguments. Values that aren't known statically are recorded as
unknown, with the reason.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ...model import Scope, ToolUse, Value
from .static import Call, Func, Item, Member, Ref, Template, Unknown, callee_name

# ---------------------------------------------------------------------------
# Drafts: what a profile extracts from one constructor call.


@dataclass
class ToolDraft:
    use: ToolUse
    agent_call: Optional[Call] = None
    """Set when the tool is another agent, resolved to its agent id once all agents are known."""


@dataclass
class AgentDraft:
    framework: str
    call: Call
    name: Optional[str] = None
    description: Optional[str] = None
    model: Value[str] = field(default_factory=Value[str].unknown)
    provider: Value[str] = field(default_factory=Value[str].unknown)
    parameters: dict[str, Any] = field(default_factory=dict)
    instructions: Any = None
    """A str, a Template, an Unknown, or None when the constructor has no instructions argument."""
    instructions_rule: Optional[str] = None
    """Set when the framework builds the instructions from other arguments, by this rule."""
    tools: list[ToolDraft] = field(default_factory=list)
    knowledge: list[tuple[str, str, Optional[str]]] = field(default_factory=list)
    """(ref, type, name) of knowledge sources."""
    extension: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers shared by profiles.

_PY_TYPES = {"str": "string", "int": "integer", "float": "number", "bool": "boolean", "list": "array", "dict": "object"}
_CONTEXT_TYPES = ("RunContextWrapper", "ToolContext", "CallbackContext", "InjectedToolArg", "ToolRuntime", "Runtime")


def _annotation_schema(node: Optional[ast.AST]) -> dict[str, Any]:
    if node is None:
        return {}
    if isinstance(node, ast.Name) and node.id in _PY_TYPES:
        return {"type": _PY_TYPES[node.id]}
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id in ("list", "List"):
        return {"type": "array", "items": _annotation_schema(node.slice)}
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id in ("dict", "Dict"):
        return {"type": "object"}
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        options = [s for s in (_annotation_schema(node.left), _annotation_schema(node.right)) if s]
        return options[0] if len(options) == 1 else {}
    return {}


def function_schema(func: Func) -> Optional[dict[str, Any]]:
    """The input schema a framework derives from a tool function's signature."""
    args = func.node.args
    positional = [*args.posonlyargs, *args.args]
    defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for arg, default in [*zip(positional, defaults), *zip(args.kwonlyargs, args.kw_defaults)]:
        if arg.arg in ("self", "cls"):
            continue
        annotation = ast.unparse(arg.annotation) if arg.annotation is not None else ""
        if any(t in annotation for t in _CONTEXT_TYPES) or arg.arg in ("ctx", "context", "tool_context", "runtime"):
            continue
        properties[arg.arg] = _annotation_schema(arg.annotation)
        if default is None:
            required.append(arg.arg)
    if not properties:
        return None
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _first_paragraph(doc: Optional[str]) -> Optional[str]:
    if not doc:
        return None
    return doc.strip().split("\n\n", 1)[0].strip() or None


def function_tool(func: Func, name: Any = None, description: Any = None) -> ToolUse:
    tool_name = name if isinstance(name, str) else func.name
    use = ToolUse(
        ref=f"tool/python.{func.qualname.rsplit('.', 1)[0]}:{func.name}",
        name=tool_name,
        description=description if isinstance(description, str) else _first_paragraph(func.docstring()),
        input_schema=function_schema(func),
    )
    return use


def decorator_calls(func: Func, evaluator: Any) -> list[tuple[Optional[str], dict[str, Any], tuple[Any, ...]]]:
    """Each decorator on ``func`` as (qualified name, keyword arguments, positional arguments)."""
    scope = evaluator.module_scope(func.module)
    out = []
    for node in func.node.decorator_list:
        value = evaluator.eval(node, scope)
        if isinstance(value, Call):
            out.append((value.qualname, value.kwargs, value.args))
        else:
            out.append((callee_name(value), {}, ()))
    return out


def string_value(value: Any) -> Optional[str]:
    return value if isinstance(value, str) else None


def model_from_string(text: str, separator: str) -> tuple[Value[str], Value[str]]:
    """``openai:gpt-5`` (LangChain) or ``openai/gpt-4o`` (LiteLLM) → (model, provider)."""
    provider, sep, model = text.partition(separator)
    if sep and provider and model:
        return Value[str].declared(model), Value[str].derived(provider.lower(), f"model string prefix {provider}{separator}")
    return Value[str].declared(text), Value[str].unknown("the model string names no provider")


def unknown_value(value: Any, what: str) -> Value[str]:
    if isinstance(value, Unknown):
        return Value[str].unknown(f"{what}: {value}")
    if value is None:
        return Value[str].unknown(f"{what} is not set")
    return Value[str].unknown(f"{what} is computed at run time")


def mcp_url(value: Any) -> Optional[str]:
    """The server URL or command of an MCP server construction, from its common argument shapes."""
    if not isinstance(value, Call):
        return None
    for key in ("url", "server_url", "command"):
        if isinstance(value.kwargs.get(key), str):
            return value.kwargs[key]
    for key in ("params", "connection_params", "server_params", "tool_config"):
        inner = value.kwargs.get(key)
        if isinstance(inner, dict):
            for k in ("url", "server_url", "command"):
                if isinstance(inner.get(k), str):
                    return inner[k]
        elif isinstance(inner, Call):
            found = mcp_url(inner)
            if found:
                return found
    if value.args and isinstance(value.args[0], str):
        return value.args[0]
    return None


def _tail(qualname: Optional[str]) -> str:
    return (qualname or "").rsplit(".", 1)[-1]


# Tool classes known to every profile.
_DATABRICKS_UC_TOOLKIT = "UCFunctionToolkit"
_VECTOR_SEARCH_TOOLS = ("VectorSearchRetrieverTool",)
_MCP_CLASSES = (
    "MCPServerStreamableHttp", "MCPServerSse", "MCPServerStdio", "McpServer", "DatabricksMCPServer",
    "MCPToolset", "McpToolset", "HostedMCPTool",
)


def common_tool(value: Any, draft: AgentDraft, where: str) -> bool:
    """Tools recognized in every framework. Returns True when ``value`` was handled."""
    if isinstance(value, Member) and value.attr == "tools" and isinstance(value.base, Call) \
            and _tail(value.base.qualname) == _DATABRICKS_UC_TOOLKIT:
        names = value.base.kwargs.get("function_names")
        if isinstance(names, list) and all(isinstance(n, str) for n in names):
            for name in names:
                ref = f"tool/databricks.uc.function.{name}"
                draft.tools.append(ToolDraft(ToolUse(ref=ref, name=name.rsplit(".", 1)[-1])))
        else:
            draft.warnings.append(f"{where}: UCFunctionToolkit function names are computed at run time")
        return True
    if isinstance(value, Call) and _tail(value.qualname) in _VECTOR_SEARCH_TOOLS:
        index = value.kwargs.get("index_name")
        if isinstance(index, str):
            draft.knowledge.append((f"knowledge/databricks.uc.vector_index.{index}", "vector_index", index))
            draft.tools.append(ToolDraft(ToolUse(
                ref=f"tool/databricks.vector_search.{index}",
                name=string_value(value.kwargs.get("tool_name")),
                description=string_value(value.kwargs.get("tool_description")),
                scope=Value[Scope].derived(Scope.READ, "vector search retrieval reads an index"),
                reversible=Value[bool].derived(True, "vector search retrieval reads an index"),
            )))
        return True
    if isinstance(value, Call) and _tail(value.qualname) in _MCP_CLASSES:
        server = mcp_url(value)
        if server:
            draft.tools.append(ToolDraft(ToolUse(ref=f"tool/mcp.{server}", name=string_value(value.kwargs.get("name")))))
        else:
            draft.warnings.append(f"{where}: MCP server address is computed at run time")
        return True
    return False


def add_tool_values(
    values: Any,
    draft: AgentDraft,
    where: str,
    handle: Callable[[Any, AgentDraft, str], bool],
) -> None:
    if isinstance(values, Unknown):
        draft.warnings.append(f"{where}: tools are {values}")
        draft.extension.setdefault("tools_unresolved", []).append(str(values))
        return
    if not isinstance(values, list):
        return
    for value in values:
        if isinstance(value, Unknown):
            draft.warnings.append(f"{where}: a tool is {value}")
            draft.extension.setdefault("tools_unresolved", []).append(str(value))
            continue
        if handle(value, draft, where) or common_tool(value, draft, where):
            continue
        if isinstance(value, Call) and value.qualname:
            # An instantiated tool class: the class is the tool's identity.
            draft.tools.append(ToolDraft(ToolUse(ref=f"tool/python.{value.qualname}", name=_tail(value.qualname))))
        elif isinstance(value, Ref):
            draft.tools.append(ToolDraft(ToolUse(ref=f"tool/python.{value.qualname}", name=_tail(value.qualname))))
        elif isinstance(value, Func):
            draft.tools.append(ToolDraft(function_tool(value)))
        else:
            draft.warnings.append(f"{where}: unrecognized tool {type(value).__name__}")


# ---------------------------------------------------------------------------
# Profiles.


class Profile:
    framework: str
    constructors: tuple[str, ...]
    orchestration: tuple[str, ...] = ()

    def __init__(self, evaluator: Any, project_calls: list[Call]):
        self.evaluator = evaluator
        self.project_calls = project_calls

    def matches(self, qualname: Optional[str]) -> bool:
        return qualname in self.constructors

    def draft(self, call: Call) -> AgentDraft:
        raise NotImplementedError


class OpenAIAgentsProfile(Profile):
    framework = "openai-agents"
    constructors = ("agents.Agent", "agents.agent.Agent")
    _TOOL_DECORATORS = ("agents.function_tool", "agents.tool", "agents.decorators.tool", "agents.tool.function_tool")
    _HOSTED = {
        "WebSearchTool": ("tool/openai.web_search", Scope.READ),
        "CodeInterpreterTool": ("tool/openai.code_interpreter", Scope.EXECUTE),
        "ImageGenerationTool": ("tool/openai.image_generation", Scope.EXECUTE),
        "ComputerTool": ("tool/openai.computer", Scope.WRITE),
        "LocalShellTool": ("tool/openai.local_shell", Scope.EXECUTE),
    }

    def _default_provider(self) -> Value[str]:
        for call in self.project_calls:
            if call.qualname == "agents.set_default_openai_client" and call.args:
                client = call.args[0]
                name = client.qualname if isinstance(client, Call) else None
                if name and name.startswith("databricks_openai"):
                    return Value[str].derived("databricks", f"default client {_tail(name)} at {call.where}")
                if name and "Azure" in name:
                    return Value[str].derived("azure-openai", f"default client {_tail(name)} at {call.where}")
                return Value[str].unknown(f"default client set at {call.where}")
        return Value[str].derived("openai", "the OpenAI Agents SDK's default client")

    def _tool(self, value: Any, draft: AgentDraft, where: str) -> bool:
        if isinstance(value, Func):
            for qualname, kwargs, _ in decorator_calls(value, self.evaluator):
                if qualname in self._TOOL_DECORATORS:
                    use = function_tool(value, kwargs.get("name_override"), kwargs.get("description_override"))
                    if isinstance(kwargs.get("needs_approval"), bool):
                        use.requires_confirmation = Value[bool].declared(kwargs["needs_approval"], f"{value.where} needs_approval")
                    draft.tools.append(ToolDraft(use))
                    return True
            return False
        if isinstance(value, Call) and _tail(value.qualname) in self._HOSTED:
            ref, scope = self._HOSTED[_tail(value.qualname)]
            draft.tools.append(ToolDraft(ToolUse(
                ref=ref, name=_tail(value.qualname), scope=Value[Scope].derived(scope, f"{_tail(value.qualname)} is a hosted tool")
            )))
            return True
        if isinstance(value, Call) and _tail(value.qualname) == "FileSearchTool":
            for store in value.kwargs.get("vector_store_ids") or []:
                if isinstance(store, str):
                    draft.knowledge.append((f"knowledge/openai.vector_store.{store}", "vector_store", store))
            draft.tools.append(ToolDraft(ToolUse(
                ref="tool/openai.file_search", name="FileSearchTool",
                scope=Value[Scope].derived(Scope.READ, "file search reads vector stores"),
                reversible=Value[bool].derived(True, "file search reads vector stores"),
            )))
            return True
        if isinstance(value, Call) and isinstance(value.func, Member) and value.func.attr == "as_tool" \
                and isinstance(value.func.base, Call) and self.matches(value.func.base.qualname):
            draft.tools.append(ToolDraft(
                ToolUse(ref="", name=string_value(value.kwargs.get("tool_name")),
                        description=string_value(value.kwargs.get("tool_description"))),
                agent_call=value.func.base,
            ))
            return True
        return False

    def draft(self, call: Call) -> AgentDraft:
        draft = AgentDraft(self.framework, call)
        kw = call.kwargs
        draft.name = string_value(kw.get("name"))
        draft.description = string_value(kw.get("handoff_description"))
        model = kw.get("model")
        if isinstance(model, str):
            if model.startswith("litellm/"):
                draft.model, draft.provider = model_from_string(model.removeprefix("litellm/"), "/")
            else:
                draft.model, draft.provider = Value[str].declared(model), self._default_provider()
        elif isinstance(model, Call) and isinstance(model.kwargs.get("model"), str):
            if _tail(model.qualname) == "LitellmModel":
                draft.model, draft.provider = model_from_string(model.kwargs["model"], "/")
            else:
                draft.model = Value[str].declared(model.kwargs["model"])
                client = model.kwargs.get("openai_client")
                name = client.qualname if isinstance(client, Call) else None
                draft.provider = (
                    Value[str].derived("databricks", f"client {_tail(name)}") if name and name.startswith("databricks_openai")
                    else Value[str].unknown("model client is configured at run time")
                )
        else:
            draft.model = unknown_value(model, "model")
            if model is None:
                draft.model = Value[str].unknown("the SDK's default model is used")
                draft.provider = self._default_provider()
        settings = kw.get("model_settings")
        if isinstance(settings, Call):
            draft.parameters = {k: v for k, v in settings.kwargs.items()
                                if k in ("temperature", "top_p", "max_tokens") and isinstance(v, (int, float))}
        instructions = kw.get("instructions")
        draft.instructions = Unknown(f"instructions are computed by {instructions.name}", instructions.where) \
            if isinstance(instructions, Func) else instructions
        add_tool_values(kw.get("tools"), draft, call.where, self._tool)
        for server in kw.get("mcp_servers") if isinstance(kw.get("mcp_servers"), list) else []:
            if not common_tool(server, draft, call.where):
                draft.warnings.append(f"{call.where}: an MCP server is {server}")
        if isinstance(kw.get("mcp_servers"), Unknown):
            draft.extension["mcp_servers_unresolved"] = str(kw["mcp_servers"])
        handoffs = kw.get("handoffs")
        for target in handoffs if isinstance(handoffs, list) else []:
            if isinstance(target, Call) and self.matches(target.qualname):
                draft.tools.append(ToolDraft(ToolUse(ref=""), agent_call=target))
            elif isinstance(target, Call) and _tail(target.qualname) == "handoff":
                agent = target.kwargs.get("agent") or (target.args[0] if target.args else None)
                if isinstance(agent, Call) and self.matches(agent.qualname):
                    draft.tools.append(ToolDraft(
                        ToolUse(ref="", name=string_value(target.kwargs.get("tool_name_override"))), agent_call=agent
                    ))
        for key in ("output_type", "tool_use_behavior", "input_guardrails", "output_guardrails"):
            if key in kw:
                draft.extension[key] = _describe_value(kw[key])
        return draft


class LangChainProfile(Profile):
    framework = "langchain"
    constructors = (
        "langchain.agents.create_agent",
        "langgraph.prebuilt.create_react_agent",
        "langgraph.prebuilt.chat_agent_executor.create_react_agent",
    )
    _CHAT_MODELS = {
        "ChatOpenAI": ("openai", ("model", "model_name")),
        "AzureChatOpenAI": ("azure-openai", ("azure_deployment", "model", "model_name")),
        "ChatAnthropic": ("anthropic", ("model", "model_name")),
        "ChatDatabricks": ("databricks", ("endpoint", "model")),
        "ChatVertexAI": ("google", ("model", "model_name")),
        "ChatGoogleGenerativeAI": ("google", ("model",)),
        "ChatBedrock": ("bedrock", ("model_id", "model")),
        "ChatBedrockConverse": ("bedrock", ("model", "model_id")),
        "ChatOllama": ("ollama", ("model",)),
        "ChatMistralAI": ("mistral", ("model",)),
        "ChatGroq": ("groq", ("model",)),
    }
    _TOOL_DECORATORS = ("langchain_core.tools.tool", "langchain.tools.tool", "langchain.agents.tool", "langchain_core.tools.convert.tool")

    def _model(self, draft: AgentDraft, model: Any) -> None:
        if isinstance(model, Member) and model.attr in ("bind_tools", "with_config"):
            model = model.base
        if isinstance(model, str):
            draft.model, draft.provider = model_from_string(model, ":")
            return
        if isinstance(model, Call):
            tail = _tail(model.qualname)
            if tail in self._CHAT_MODELS:
                provider, keys = self._CHAT_MODELS[tail]
                draft.provider = Value[str].derived(provider, f"{tail} chat model")
                for key in keys:
                    if isinstance(model.kwargs.get(key), str):
                        draft.model = Value[str].declared(model.kwargs[key], model.where)
                        break
                else:
                    draft.model = unknown_value(next((model.kwargs[k] for k in keys if k in model.kwargs), None), "model")
                draft.parameters = {k: v for k, v in model.kwargs.items()
                                    if k in ("temperature", "max_tokens", "top_p") and isinstance(v, (int, float))}
                return
            if tail == "init_chat_model":
                name = model.args[0] if model.args else model.kwargs.get("model")
                if isinstance(name, str):
                    if isinstance(model.kwargs.get("model_provider"), str):
                        draft.model = Value[str].declared(name)
                        draft.provider = Value[str].declared(model.kwargs["model_provider"].lower())
                    else:
                        draft.model, draft.provider = model_from_string(name, ":")
                    return
        draft.model = unknown_value(model, "model")

    def _tool(self, value: Any, draft: AgentDraft, where: str) -> bool:
        if isinstance(value, Func):
            for qualname, kwargs, args in decorator_calls(value, self.evaluator):
                if qualname in self._TOOL_DECORATORS:
                    name = args[0] if args and isinstance(args[0], str) else kwargs.get("name_or_callable")
                    draft.tools.append(ToolDraft(function_tool(value, name, kwargs.get("description"))))
                    return True
        return False

    def draft(self, call: Call) -> AgentDraft:
        draft = AgentDraft(self.framework, call)
        kw = call.kwargs
        model = kw.get("model", call.args[0] if call.args else None)
        tools = kw.get("tools", call.args[1] if len(call.args) > 1 else None)
        self._model(draft, model)
        prompt_key = "system_prompt" if call.qualname == "langchain.agents.create_agent" else "prompt"
        draft.instructions = kw.get(prompt_key)
        if isinstance(draft.instructions, Call):
            draft.instructions = Unknown("a prompt object built at run time", call.where)
        draft.name = string_value(kw.get("name"))
        add_tool_values(tools, draft, call.where, self._tool)
        for key in ("middleware", "response_format", "checkpointer", "state_schema"):
            if key in kw:
                draft.extension[key] = _describe_value(kw[key])
        return draft


class GoogleAdkProfile(Profile):
    framework = "google-adk"
    constructors = (
        "google.adk.Agent", "google.adk.agents.Agent", "google.adk.agents.LlmAgent",
        "google.adk.agents.llm_agent.Agent", "google.adk.agents.llm_agent.LlmAgent",
    )
    orchestration = ("SequentialAgent", "ParallelAgent", "LoopAgent")
    _BUILTINS = {
        "google_search": ("tool/google.search", Scope.READ),
        "built_in_code_execution": ("tool/google.code_execution", Scope.EXECUTE),
        "url_context": ("tool/google.url_context", Scope.READ),
        "VertexAiSearchTool": ("tool/google.vertex_ai_search", Scope.READ),
    }

    def matches(self, qualname: Optional[str]) -> bool:
        return qualname in self.constructors

    def _tool(self, value: Any, draft: AgentDraft, where: str) -> bool:
        name = value.qualname if isinstance(value, (Ref, Call)) else None
        if name and name.startswith("google.adk.tools") and _tail(name) in self._BUILTINS:
            ref, scope = self._BUILTINS[_tail(name)]
            draft.tools.append(ToolDraft(ToolUse(ref=ref, name=_tail(name), scope=Value[Scope].derived(scope, f"{_tail(name)} built-in tool"))))
            return True
        if isinstance(value, Call) and _tail(value.qualname) == "AgentTool":
            agent = value.kwargs.get("agent") or (value.args[0] if value.args else None)
            if isinstance(agent, Call) and self.matches(agent.qualname):
                draft.tools.append(ToolDraft(ToolUse(ref=""), agent_call=agent))
                return True
        if isinstance(value, Func):
            # ADK turns any plain function into a tool.
            draft.tools.append(ToolDraft(function_tool(value)))
            return True
        return False

    def draft(self, call: Call) -> AgentDraft:
        draft = AgentDraft(self.framework, call)
        kw = call.kwargs
        draft.name = string_value(kw.get("name"))
        draft.description = string_value(kw.get("description"))
        model = kw.get("model")
        if isinstance(model, str):
            draft.model = Value[str].declared(model)
            draft.provider = (Value[str].derived("google", "Gemini model name") if model.startswith("gemini")
                              else Value[str].unknown("the model name names no provider"))
        elif isinstance(model, Call) and _tail(model.qualname) == "LiteLlm" and isinstance(model.kwargs.get("model"), str):
            draft.model, draft.provider = model_from_string(model.kwargs["model"], "/")
        else:
            draft.model = unknown_value(model, "model")
        config = kw.get("generate_content_config")
        if isinstance(config, Call):
            mapping = {"temperature": "temperature", "top_p": "top_p", "max_output_tokens": "max_tokens"}
            draft.parameters = {mapping[k]: v for k, v in config.kwargs.items() if k in mapping and isinstance(v, (int, float))}
        instruction = kw.get("instruction")
        draft.instructions = Unknown(f"instructions are computed by {instruction.name}", instruction.where) \
            if isinstance(instruction, Func) else instruction
        add_tool_values(kw.get("tools"), draft, call.where, self._tool)
        for sub in kw.get("sub_agents") if isinstance(kw.get("sub_agents"), list) else []:
            if isinstance(sub, Call) and (self.matches(sub.qualname) or _tail(sub.qualname) in self.orchestration):
                draft.tools.append(ToolDraft(ToolUse(ref=""), agent_call=sub))
        for key in ("output_key", "output_schema", "before_model_callback", "after_model_callback", "planner"):
            if key in kw:
                draft.extension[key] = _describe_value(kw[key])
        return draft


class CrewAIProfile(Profile):
    framework = "crewai"
    constructors = ("crewai.Agent", "crewai.agent.Agent")

    def __init__(self, evaluator: Any, project_calls: list[Call], configs: dict[str, dict[str, Any]]):
        super().__init__(evaluator, project_calls)
        self.configs = configs

    def draft(self, call: Call) -> AgentDraft:
        draft = AgentDraft(self.framework, call)
        kw = dict(call.kwargs)
        config = kw.get("config")
        if isinstance(config, Item) and isinstance(config.key, str) and isinstance(config.base, Ref) \
                and config.base.qualname == "self.agents_config":
            values = self.configs.get(call.module.rel, {}).get(config.key)
            if values is None:
                draft.warnings.append(f"{call.where}: agent config {config.key!r} not found in config/agents.yaml")
            else:
                draft.extension["config"] = f"config/agents.yaml#{config.key}"
                for key, value in values.items():
                    kw.setdefault(key, value)
                kw.setdefault("name", config.key)
        role, goal, backstory = (kw.get(k) for k in ("role", "goal", "backstory"))
        draft.name = string_value(kw.get("name")) or (role.strip() if isinstance(role, str) else None)
        draft.description = goal.strip() if isinstance(goal, str) else None
        parts = [(label, value) for label, value in (("Role", role), ("Goal", goal), ("Backstory", backstory)) if value is not None]
        if parts and all(isinstance(v, str) for _, v in parts):
            draft.instructions = "\n".join(f"{label}: {str(value).strip()}" for label, value in parts)
            draft.instructions_rule = "CrewAI builds the system prompt from role, goal and backstory"
        elif parts:
            draft.instructions = Unknown("role, goal or backstory computed at run time", call.where)
        llm = kw.get("llm")
        if isinstance(llm, str):
            draft.model, draft.provider = model_from_string(llm, "/")
        elif isinstance(llm, Call) and isinstance(llm.kwargs.get("model"), str):
            draft.model, draft.provider = model_from_string(llm.kwargs["model"], "/")
            draft.parameters = {k: v for k, v in llm.kwargs.items()
                                if k in ("temperature", "max_tokens", "top_p") and isinstance(v, (int, float))}
        elif llm is None:
            draft.model = Value[str].unknown("CrewAI's default model, set by the environment")
        else:
            draft.model = unknown_value(llm, "llm")
        add_tool_values(kw.get("tools"), draft, call.where, lambda *_: False)
        if isinstance(kw.get("allow_delegation"), bool):
            draft.extension["allow_delegation"] = kw["allow_delegation"]
        return draft


def _describe_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (Ref, Call, Func)):
        return callee_name(value) if not isinstance(value, Func) else value.qualname
    if isinstance(value, list):
        return [_describe_value(v) for v in value]
    return str(value) if isinstance(value, Unknown) else type(value).__name__


ORCHESTRATION_HINTS = {
    "langgraph.graph.StateGraph": "custom LangGraph StateGraph; its nodes are not analyzed",
    "langgraph.graph.state.StateGraph": "custom LangGraph StateGraph; its nodes are not analyzed",
    "crewai.Crew": "CrewAI crew; its agents are recorded",
    "google.adk.agents.SequentialAgent": "ADK workflow agent; its sub-agents are recorded",
    "google.adk.agents.ParallelAgent": "ADK workflow agent; its sub-agents are recorded",
    "google.adk.agents.LoopAgent": "ADK workflow agent; its sub-agents are recorded",
}


# ---------------------------------------------------------------------------
# Agents built directly on model APIs, without a framework.

# Client class → (API, provider, model-call methods). The provider is who serves the model.
_RAW_CLIENTS: dict[str, tuple[str, Optional[str], tuple[str, ...]]] = {
    "openai.OpenAI": ("openai", "openai", ("chat.completions.create", "chat.completions.parse", "beta.chat.completions.parse", "responses.create")),
    "openai.AsyncOpenAI": ("openai", "openai", ("chat.completions.create", "chat.completions.parse", "beta.chat.completions.parse", "responses.create")),
    "openai.AzureOpenAI": ("openai", "azure-openai", ("chat.completions.create", "responses.create")),
    "openai.AsyncAzureOpenAI": ("openai", "azure-openai", ("chat.completions.create", "responses.create")),
    "openai": ("openai", "openai", ("chat.completions.create", "responses.create")),
    "databricks_openai.DatabricksOpenAI": ("openai", "databricks", ("chat.completions.create", "responses.create")),
    "databricks_openai.AsyncDatabricksOpenAI": ("openai", "databricks", ("chat.completions.create", "responses.create")),
    "anthropic.Anthropic": ("anthropic", "anthropic", ("messages.create", "messages.stream", "beta.messages.create")),
    "anthropic.AsyncAnthropic": ("anthropic", "anthropic", ("messages.create", "messages.stream", "beta.messages.create")),
    "anthropic.AnthropicBedrock": ("anthropic", "bedrock", ("messages.create", "messages.stream")),
    "anthropic.AsyncAnthropicBedrock": ("anthropic", "bedrock", ("messages.create", "messages.stream")),
    "anthropic.AnthropicVertex": ("anthropic", "google", ("messages.create", "messages.stream")),
    "anthropic.AsyncAnthropicVertex": ("anthropic", "google", ("messages.create", "messages.stream")),
    "google.genai.Client": ("google", "google", ("models.generate_content", "models.generate_content_stream", "aio.models.generate_content")),
    "litellm": ("litellm", None, ("completion", "acompletion")),
    "boto3.client": ("bedrock", "bedrock", ("converse", "converse_stream")),
}
RAW_CALLS = {f"{client}.{method}": client for client, (_, _, methods) in _RAW_CLIENTS.items() for method in methods}
# When the client can't be resolved statically (for example it is chosen at run time), the SDK the
# module imports and the method chain still identify the API.
_SDK_METHODS = {
    "anthropic": ("anthropic", ("messages.create", "messages.stream", "beta.messages.create", "beta.messages.stream")),
    "openai": ("openai", ("chat.completions.create", "chat.completions.parse", "responses.create")),
    "google.genai": ("google", ("models.generate_content", "models.generate_content_stream")),
}
for _sdk, (_api, _methods) in _SDK_METHODS.items():
    _RAW_CLIENTS[f"{_sdk}:unresolved"] = (_api, None, _methods)


def normalize_call_name(name: Optional[str]) -> Optional[str]:
    """Drop SDK response wrappers: ``messages.with_raw_response.create`` is ``messages.create``."""
    if not name:
        return name
    return name.replace(".with_raw_response", "").replace(".with_streaming_response", "")


def unresolved_client(chain: str, imported: set[str]) -> Optional[str]:
    """The SDK behind a model call whose client couldn't be resolved, from its method chain."""
    chain = normalize_call_name(chain) or ""
    for sdk, (_, methods) in _SDK_METHODS.items():
        if sdk in imported and any(chain.endswith("." + method) for method in methods):
            return f"{sdk}:unresolved"
    return None
_PLATFORMS = {"openai": "openai-api", "anthropic": "anthropic-api", "google": "google-genai", "litellm": "litellm", "bedrock": "bedrock-runtime"}
_HOSTED_RAW = {
    "web_search": ("tool/openai.web_search", Scope.READ), "web_search_preview": ("tool/openai.web_search", Scope.READ),
    "code_interpreter": ("tool/openai.code_interpreter", Scope.EXECUTE), "computer_use_preview": ("tool/openai.computer", Scope.WRITE),
    "image_generation": ("tool/openai.image_generation", Scope.EXECUTE),
}


def _client_call(value: Any) -> Optional[Call]:
    """The client construction at the root of ``client.chat.completions.create``."""
    while isinstance(value, Member):
        value = value.base
    return value if isinstance(value, Call) else None


class RawModelCallProfile:
    """A model call that passes tools is an agent loop, even without a framework.

    The agent is inferred from the call site, and its record says so.
    """

    def __init__(self, evaluator: Any):
        self.evaluator = evaluator

    def matches(self, qualname: Optional[str]) -> bool:
        return qualname in RAW_CALLS

    def is_agent(self, call: Call) -> bool:
        if call.qualname and normalize_call_name(call.qualname).startswith("boto3.client."):
            client = _client_call(call.func)
            if not (client and client.args and client.args[0] == "bedrock-runtime"):
                return False
            config = call.kwargs.get("toolConfig")
            return isinstance(config, dict) and bool(config.get("tools"))
        if call.qualname and call.qualname.startswith("google.genai."):
            config = call.kwargs.get("config")
            return isinstance(config, Call) and bool(config.kwargs.get("tools"))
        tools = call.kwargs.get("tools")
        return bool(tools) and not isinstance(tools, Unknown) or bool(call.kwargs.get("mcp_servers"))

    def draft(self, call: Call, function: Optional[str], client_name: str) -> AgentDraft:
        api, provider, _ = _RAW_CLIENTS[client_name]
        draft = AgentDraft(_PLATFORMS[api], call)
        draft.name = function.rsplit(".", 1)[-1] if function else None
        draft.extension["inferred"] = "a model call that passes tools"
        draft.extension["api"] = normalize_call_name(call.qualname) or ast.unparse(call.node.func)
        kw = call.kwargs
        client = _client_call(call.func)

        model = kw.get("modelId") if api == "bedrock" else kw.get("model")
        if api == "litellm" and isinstance(model, str):
            draft.model, draft.provider = model_from_string(model, "/")
        elif isinstance(model, str):
            draft.model = Value[str].declared(model, call.where)
            if client is not None and client.kwargs.get("base_url") is not None:
                draft.provider = Value[str].unknown(f"client base_url set at {client.where}")
            elif provider:
                draft.provider = Value[str].derived(provider, f"{_tail(client_name)} client")
            else:
                draft.provider = Value[str].unknown("the client is chosen at run time")
        else:
            draft.model = unknown_value(model, "model")
            if client is not None and provider and client.kwargs.get("base_url") is None:
                draft.provider = Value[str].derived(provider, f"{_tail(client_name)} client")
        if not draft.provider.is_known and not draft.provider.basis:
            draft.provider = Value[str].unknown("the client is chosen at run time")

        parameters = {"temperature": kw.get("temperature"), "top_p": kw.get("top_p"),
                      "max_tokens": kw.get("max_tokens", kw.get("max_output_tokens", kw.get("max_completion_tokens")))}
        if api == "google" and isinstance(kw.get("config"), Call):
            config = kw["config"].kwargs
            parameters.update(temperature=config.get("temperature"), top_p=config.get("top_p"), max_tokens=config.get("max_output_tokens"))
        if api == "bedrock" and isinstance(kw.get("inferenceConfig"), dict):
            config = kw["inferenceConfig"]
            parameters.update(temperature=config.get("temperature"), top_p=config.get("topP"), max_tokens=config.get("maxTokens"))
        draft.parameters = {k: v for k, v in parameters.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}

        draft.instructions = self._instructions(api, call)
        self._tools(api, call, draft)
        return draft

    def _instructions(self, api: str, call: Call) -> Any:
        kw = call.kwargs
        if api == "anthropic":
            system = kw.get("system")
            if isinstance(system, list):
                texts = [b.get("text") if isinstance(b, dict) else b.kwargs.get("text") if isinstance(b, Call) else None
                         for b in system]
                if any(isinstance(t, Template) for t in texts) and all(isinstance(t, (str, Template)) for t in texts):
                    return texts[0] if len(texts) == 1 else Template(tuple(p for t in texts for p in (t.parts if isinstance(t, Template) else (t,))))
                return "\n\n".join(t for t in texts if isinstance(t, str)) if all(isinstance(t, str) for t in texts) else Unknown("system blocks computed at run time", call.where)
            return system
        if api == "google":
            config = kw.get("config")
            return config.kwargs.get("system_instruction") if isinstance(config, Call) else None
        if api == "bedrock":
            system = kw.get("system")
            if isinstance(system, list):
                texts = [b.get("text") for b in system if isinstance(b, dict)]
                return "\n\n".join(t for t in texts if isinstance(t, str)) or None
            return system
        if call.qualname and call.qualname.endswith("responses.create") and kw.get("instructions") is not None:
            return kw["instructions"]
        messages = kw.get("messages") if "messages" in kw else kw.get("input")
        if isinstance(messages, list):
            for message in messages:
                if isinstance(message, dict) and message.get("role") in ("system", "developer"):
                    return message.get("content")
            return None
        return Unknown("messages built at run time", call.where) if messages is not None else None

    def _tools(self, api: str, call: Call, draft: AgentDraft) -> None:
        kw = call.kwargs
        module = call.module.name
        if api == "bedrock":
            config = kw.get("toolConfig") or {}
            tools = [t.get("toolSpec") for t in config.get("tools") or [] if isinstance(t, dict)] if isinstance(config, dict) else []
            for spec in tools:
                if isinstance(spec, dict) and isinstance(spec.get("name"), str):
                    schema = (spec.get("inputSchema") or {}).get("json")
                    draft.tools.append(ToolDraft(ToolUse(ref=f"tool/python.{module}:{spec['name']}", name=spec["name"],
                                                         description=string_value(spec.get("description")),
                                                         input_schema=schema if isinstance(schema, dict) else None)))
            return
        if api == "google":
            config = kw.get("config")
            tools = config.kwargs.get("tools") if isinstance(config, Call) else None
            for tool in tools if isinstance(tools, list) else []:
                if isinstance(tool, Func):
                    draft.tools.append(ToolDraft(function_tool(tool)))
                elif isinstance(tool, Call):
                    if tool.kwargs.get("google_search") is not None:
                        draft.tools.append(ToolDraft(ToolUse(ref="tool/google.search", name="google_search",
                                                             scope=Value[Scope].derived(Scope.READ, "Google Search grounding"))))
                    for declaration in tool.kwargs.get("function_declarations") or []:
                        if isinstance(declaration, Call) and isinstance(declaration.kwargs.get("name"), str):
                            name = declaration.kwargs["name"]
                            params = declaration.kwargs.get("parameters")
                            draft.tools.append(ToolDraft(ToolUse(ref=f"tool/python.{module}:{name}", name=name,
                                                                 description=string_value(declaration.kwargs.get("description")),
                                                                 input_schema=params if isinstance(params, dict) else None)))
            return
        tools = kw.get("tools")
        if tools is not None and not isinstance(tools, list):
            draft.warnings.append(f"{call.where}: tools are built at run time")
            draft.extension.setdefault("tools_unresolved", []).append(
                str(tools) if isinstance(tools, Unknown) else f"built by {callee_name(tools.func) if isinstance(tools, Call) else 'code'} at {call.where}"
            )
        for tool in tools if isinstance(tools, list) else []:
            if isinstance(tool, Func):
                draft.tools.append(ToolDraft(function_tool(tool)))
                continue
            if not isinstance(tool, dict):
                draft.warnings.append(f"{call.where}: a tool is computed at run time")
                continue
            kind = tool.get("type")
            spec = tool.get("function") if isinstance(tool.get("function"), dict) else tool
            if kind in _HOSTED_RAW:
                ref, scope = _HOSTED_RAW[kind]
                draft.tools.append(ToolDraft(ToolUse(ref=ref, name=kind, scope=Value[Scope].derived(scope, f"hosted tool {kind}"))))
            elif kind == "file_search":
                for store in tool.get("vector_store_ids") or []:
                    if isinstance(store, str):
                        draft.knowledge.append((f"knowledge/openai.vector_store.{store}", "vector_store", store))
                draft.tools.append(ToolDraft(ToolUse(ref="tool/openai.file_search", name="file_search",
                                                     scope=Value[Scope].derived(Scope.READ, "file search reads vector stores"))))
            elif kind == "mcp" and isinstance(tool.get("server_url"), str):
                draft.tools.append(ToolDraft(ToolUse(ref=f"tool/mcp.{tool['server_url']}", name=string_value(tool.get("server_label")))))
            elif isinstance(kind, str) and kind.startswith(("web_search_", "code_execution_", "bash_", "text_editor_", "computer_")):
                # Anthropic server and client tools, versioned by date.
                base = kind.rsplit("_", 1)[0]
                scope = Scope.READ if base == "web_search" else Scope.WRITE if base in ("text_editor", "computer") else Scope.EXECUTE
                draft.tools.append(ToolDraft(ToolUse(ref=f"tool/anthropic.{base}", name=string_value(tool.get("name")) or base,
                                                     scope=Value[Scope].derived(scope, f"Anthropic tool {kind}"))))
            elif isinstance(spec.get("name"), str):
                schema = spec.get("parameters", spec.get("input_schema"))
                draft.tools.append(ToolDraft(ToolUse(ref=f"tool/python.{module}:{spec['name']}", name=spec["name"],
                                                     description=string_value(spec.get("description")),
                                                     input_schema=schema if isinstance(schema, dict) else None)))
        for server in kw.get("mcp_servers") or [] if isinstance(kw.get("mcp_servers"), list) else []:
            if isinstance(server, dict) and isinstance(server.get("url"), str):
                draft.tools.append(ToolDraft(ToolUse(ref=f"tool/mcp.{server['url']}", name=string_value(server.get("name")))))
