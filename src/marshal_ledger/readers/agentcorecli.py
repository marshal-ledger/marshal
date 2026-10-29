"""Reader for AgentCore CLI projects: AgentCore harnesses.

An AgentCore CLI project (``agentcore create``) registers its harnesses in
``agentcore/agentcore.json`` (``harnesses: [{name, path}]``). Each harness is configured in
its own folder: ``harness.yaml`` in projects of schema version 2, or ``harness.json`` in
version 1 projects, with the instructions in ``system-prompt.md`` next to it unless the
file sets ``systemPrompt`` inline. ``agentcore deploy`` turns each harness into an
``AWS::BedrockAgentCore::Harness``.

``harness.yaml`` follows the Harness API's nesting in camelCase; ``harness.json`` is the
older flat shape (``model: {provider, modelId}``, ``sessionStoragePath``, ...). Both are
translated into the CloudFormation resource's properties and read by the CloudFormation
reader's harness mapping, so a harness maps the same way whichever tool defines it.
Header and environment variable values are never copied, only their names.

Projects that define runtimes (agent code) and no harness are left to the Python reader.

Mapping rules are documented in ``docs/formats/agentcore-cli.md``.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Optional

from ..model import MARSHAL_NAMESPACE
from .base import ReadOptions, ReadResult, Reader, Skipped, _SKIP_DIRS, load_document
from .cloudformation import HARNESS, CloudFormationReader, _Template

FORMAT = "agentcore-cli"
PROJECT_FILE = "agentcore/agentcore.json"
PROMPT_FILE = "system-prompt.md"
MAX_PROMPT_BYTES = 1_000_000

# The flat shape's model provider, and the configuration it nests under.
_MODEL_KEYS = {"bedrock": "bedrockModelConfig", "open_ai": "openAiModelConfig", "gemini": "geminiModelConfig",
               "lite_llm": "liteLlmModelConfig"}
_MEMORY_KEYS = {"managed": "managedMemoryConfiguration", "existing": "agentCoreMemoryConfiguration", "disabled": "disabled"}
# Maps whose keys are names the user chose, kept as written.
_VERBATIM = {"headers", "inputSchema", "additionalParams", "environmentVariables", "customParameters"}
# Settings the CLI reads that the Harness resource has no property for: the ones the
# ledger keeps, and the ones it leaves out (tags, and the VPC id CodeBuild needs).
_CLI_ONLY = {"dockerfile": "dockerfile", "connections": "connections"}
_DROPPED = {"tags", "networkConfig", "authorizerType"}


def _flat_to_nested(spec: dict[str, Any]) -> dict[str, Any]:
    """A version 1 ``harness.json`` in the version 2 ``harness.yaml`` shape, as ``agentcore migrate`` writes it."""
    out = {k: v for k, v in spec.items() if k not in (
        "model", "systemPrompt", "skills", "memory", "containerUri", "networkMode", "networkConfig", "lifecycleConfig",
        "sessionStoragePath", "efsAccessPoints", "s3AccessPoints", "authorizerConfiguration")}
    model = dict(spec.get("model") or {}) if isinstance(spec.get("model"), dict) else {}
    provider = model.pop("provider", None)
    out["model"] = {_MODEL_KEYS[provider]: model} if provider in _MODEL_KEYS else {}
    if isinstance(spec.get("systemPrompt"), str):
        out["systemPrompt"] = [{"text": spec["systemPrompt"]}]
    skills = []
    for skill in spec.get("skills") or []:
        if isinstance(skill, str):
            skills.append({"path": skill})
        elif isinstance(skill, dict) and "s3Uri" in skill:
            skills.append({"s3": {"uri": skill["s3Uri"]}})
        elif isinstance(skill, dict) and "gitUrl" in skill:
            skills.append({"git": {"url": skill["gitUrl"], **{k: v for k, v in skill.items() if k != "gitUrl"}}})
        else:
            skills.append(skill)
    if skills:
        out["skills"] = skills
    memory = spec.get("memory")
    if isinstance(memory, dict):
        settings = {k: v for k, v in memory.items() if k != "mode"}
        mode = memory.get("mode", "existing")  # a memory given by name or ARN alone is an existing one
        if mode in _MEMORY_KEYS:
            out["memory"] = {_MEMORY_KEYS[mode]: settings}
    if spec.get("containerUri"):
        out["environmentArtifact"] = {"containerConfiguration": {"containerUri": spec["containerUri"]}}
    runtime: dict[str, Any] = {}
    network = spec.get("networkConfig") if isinstance(spec.get("networkConfig"), dict) else {}
    if spec.get("networkMode"):
        mode_config = {k: v for k, v in network.items() if k in ("subnets", "securityGroups")}
        runtime["networkConfiguration"] = {"networkMode": spec["networkMode"],
                                           **({"networkModeConfig": mode_config} if mode_config else {})}
    if spec.get("lifecycleConfig"):
        runtime["lifecycleConfiguration"] = spec["lifecycleConfig"]
    mounts = ([{"sessionStorage": {"mountPath": spec["sessionStoragePath"]}}] if spec.get("sessionStoragePath") else []) + \
        [{"efsAccessPoint": a} for a in spec.get("efsAccessPoints") or []] + \
        [{"s3FilesAccessPoint": a} for a in spec.get("s3AccessPoints") or []]
    if mounts:
        runtime["filesystemConfigurations"] = mounts
    if runtime:
        out["environment"] = {"agentCoreRuntimeEnvironment": runtime}
    if network.get("vpcId"):
        out["networkConfig"] = {"vpcId": network["vpcId"]}
    jwt = (spec.get("authorizerConfiguration") or {}).get("customJwtAuthorizer") \
        if isinstance(spec.get("authorizerConfiguration"), dict) else None
    if jwt:
        out["authorizerConfiguration"] = {"customJWTAuthorizer": jwt}
    return out


def _without_defaults(spec: dict[str, Any]) -> dict[str, Any]:
    """The harness with settings equal to the CLI's documented defaults left out, so a file that
    states a default records the same as one that omits it: PUBLIC network mode with no VPC settings."""
    runtime = ((spec.get("environment") or {}).get("agentCoreRuntimeEnvironment")
               if isinstance(spec.get("environment"), dict) else None)
    if isinstance(runtime, dict) and runtime.get("networkConfiguration") == {"networkMode": "PUBLIC"}:
        runtime = {k: v for k, v in runtime.items() if k != "networkConfiguration"}
        spec = {**spec, "environment": {**spec["environment"], "agentCoreRuntimeEnvironment": runtime}}
        if not runtime and len(spec["environment"]) == 1:
            spec = {k: v for k, v in spec.items() if k != "environment"}
    return spec


def _pascal(value: Any, key: str = "") -> Any:
    """A camelCase ``harness.yaml`` value in CloudFormation's PascalCase, keeping user-chosen keys."""
    if isinstance(value, dict):
        if key in _VERBATIM:
            return value
        return {k[:1].upper() + k[1:]: _pascal(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_pascal(v, key) for v in value]
    return value


def _camel(prop: str) -> str:
    return ".".join(part[:1].lower() + part[1:] for part in prop.split("."))


class _HarnessTemplate(_Template):
    """One CLI harness translated into an ``AWS::BedrockAgentCore::Harness`` resource."""

    source_format = FORMAT
    created_role = "executionRoleArn is not set, so agentcore deploy creates an execution role"

    def __init__(self, logical: str, props: dict[str, Any], agent: str, artifact: str, *, flat: bool,
                 prompt_file: Optional[str], extras: dict[str, Any]):
        self.flat = flat
        self.prompt_file = prompt_file
        self.extras = extras
        resource = {"Type": HARNESS, "Properties": props, "Metadata": {MARSHAL_NAMESPACE: {"agent": agent}}}
        super().__init__({"Resources": {logical: resource}}, artifact)

    def path(self, logical: str, prop: str = "") -> str:
        if not prop:
            return f"harness {logical}"
        if prop == "SystemPrompt" and self.prompt_file:
            return self.prompt_file
        if prop == "HarnessName":
            return "name"
        path = _camel(prop)
        if self.flat:  # the flat shape has the model settings directly under model
            path = re.sub(r"^model\.\w+ModelConfig(?=\.|$)", "model", path)
        return path

    def detected_by(self, logical: str) -> str:
        return f"harness registered in {PROJECT_FILE}"

    def extensions(self, logical: str) -> dict[str, Any]:
        return self.extras


def _project_files(path: Path) -> Iterator[Path]:
    """The ``agentcore/agentcore.json`` files at or under ``path``."""
    if path.is_file():
        if path.name == "agentcore.json" and path.parent.name == "agentcore":
            yield path
        return
    if path.name == "agentcore" and (path / "agentcore.json").is_file():
        yield path / "agentcore.json"
        return
    for found in sorted(path.rglob("agentcore.json")):
        relative = found.relative_to(path).parts
        if found.parent.name == "agentcore" and not any(part in _SKIP_DIRS or part == "cdk.out" for part in relative):
            yield found


def _registry(file: Path) -> Optional[dict[str, Any]]:
    data = load_document(file)
    if isinstance(data, dict) and isinstance(data.get("harnesses"), list) and data["harnesses"]:
        return data
    return None


class AgentCoreCliReader(Reader):
    format = FORMAT
    platform = "bedrock-agentcore"

    @classmethod
    def detect(cls, path: Path) -> bool:
        return path.exists() and any(_registry(f) for f in _project_files(path))

    def read(self, path: Path, options: ReadOptions | None = None) -> ReadResult:
        options = options or ReadOptions()
        result = ReadResult()
        if path.is_file():
            root = path.parent.parent
        elif path.name == "agentcore" and (path / "agentcore.json").is_file():
            root = path.parent
        else:
            root = path
        for file in _project_files(path):
            project = _registry(file)
            if project is None:
                continue
            part = self._project(project, file.parent.parent, root, options)
            result.records += part.records
            result.skipped += part.skipped
            result.warnings += part.warnings
            known = {e.ref for e in result.entities}
            result.entities += [e for e in part.entities if e.ref not in known]
        return result

    def _project(self, project: dict[str, Any], folder: Path, root: Path, options: ReadOptions) -> ReadResult:
        result = ReadResult()
        registry = (folder / PROJECT_FILE).relative_to(root).as_posix()
        name = project.get("name") if isinstance(project.get("name"), str) else folder.name
        for entry in project["harnesses"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not isinstance(entry.get("path"), str):
                result.skipped.append(Skipped(registry, "harnesses", "a harness entry needs name and path"))
                continue
            relative = PurePosixPath(entry["path"].replace("\\", "/"))
            if relative.is_absolute() or ".." in relative.parts or re.match(r"^[A-Za-z]:", entry["path"]):
                result.skipped.append(Skipped(registry, f"harnesses.{entry['name']}", f"path {entry['path']} is outside the project"))
                continue
            directory = folder / relative
            # Version 2 projects configure a harness in harness.yaml; version 1 projects in harness.json.
            candidates = ["harness.json", "harness.yaml"] if project.get("version") == 1 else ["harness.yaml", "harness.json"]
            config = next((directory / c for c in candidates if (directory / c).is_file()), None)
            data = load_document(config) if config else None
            if not isinstance(data, dict):
                where = (directory.relative_to(root) / (config.name if config else "harness.yaml")).as_posix()
                result.skipped.append(Skipped(where, entry["name"], "harness configuration not found or not readable"))
                continue
            part = self._harness(entry["name"], data, config, f"{name}/{entry['name']}", root, options)
            result.records += part.records
            result.warnings += part.warnings
            known = {e.ref for e in result.entities}
            result.entities += [e for e in part.entities if e.ref not in known]
        return result

    def _harness(self, logical: str, data: dict[str, Any], config: Path, agent: str, root: Path,
                 options: ReadOptions) -> ReadResult:
        flat = config.suffix == ".json"
        spec = _without_defaults(_flat_to_nested(data) if flat else dict(data))
        artifact = config.relative_to(root).as_posix()
        prompt_file = None
        prompt = config.parent / PROMPT_FILE
        warnings = []
        if spec.get("systemPrompt") is None and prompt.is_file():
            # Without an inline systemPrompt, the CLI deploys the file next to the configuration.
            if prompt.stat().st_size <= MAX_PROMPT_BYTES:
                try:
                    spec["systemPrompt"] = [{"text": prompt.read_text(encoding="utf-8")}]
                    prompt_file = prompt.relative_to(root).as_posix()
                except (OSError, UnicodeDecodeError):
                    warnings.append(f"{artifact}: {PROMPT_FILE} is not readable text")
            else:
                warnings.append(f"{artifact}: {PROMPT_FILE} is larger than {MAX_PROMPT_BYTES} bytes and was not read")
        extras = {label: spec[key] for key, label in _CLI_ONLY.items() if spec.get(key) is not None}
        props: dict[str, Any] = {}
        for key, value in spec.items():
            if key in _CLI_ONLY or key in _DROPPED or value is None:
                continue
            props["HarnessName" if key == "name" else key[:1].upper() + key[1:]] = _pascal(value, key)
        template = _HarnessTemplate(logical, props, agent, artifact, flat=flat, prompt_file=prompt_file, extras=extras)
        result = CloudFormationReader().read_template(template, options)
        result.warnings = warnings + result.warnings
        return result


__all__ = ["FORMAT", "AgentCoreCliReader"]
