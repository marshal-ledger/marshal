"""Reader for Vertex AI Agent Engine resources, from saved API responses.

Agent Engine (the ``reasoningEngines`` resource) hosts agents written in code.
Marshal never calls the API itself: a job the organization runs where it has
access saves the output of ``reasoningEngines.get`` or ``reasoningEngines.list``
(or ``gcloud`` with ``--format=json``) and the reader reads that file.

Records are inventory tier: identity, framework, deploy source, environment
variable names and secret references, class methods. The model, instructions
and tools are in the agent's code; read the source with the Python reader.

Mapping rules are documented in ``docs/formats/vertex-agent-engine.md``.
"""

from __future__ import annotations

from typing import Any

from ..model import AgentSpec, IdentityKind, Instructions, Source, Value
from ..redact import redact
from .base import DocumentReader, ReadOptions, ReadResult, build_record, slug

FORMAT = "vertex-agent-engine"


def _engines(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [e for e in data if _is_engine(e)]
    if isinstance(data, dict):
        if isinstance(data.get("reasoningEngines"), list):
            return [e for e in data["reasoningEngines"] if _is_engine(e)]
        if _is_engine(data):
            return [data]
    return []


def _is_engine(value: Any) -> bool:
    return isinstance(value, dict) and "/reasoningEngines/" in str(value.get("name", ""))


class AgentEngineReader(DocumentReader):
    format = FORMAT
    platform = FORMAT

    @classmethod
    def matches(cls, data: Any) -> bool:
        return bool(_engines(data))

    def read_data(self, data: Any, artifact: str, options: ReadOptions | None = None) -> ReadResult:
        options = options or ReadOptions()
        result = ReadResult()
        for engine in _engines(data):
            result.records.append(self._engine(engine, artifact, options))
        return result

    def _engine(self, engine: dict[str, Any], artifact: str, options: ReadOptions):
        spec_data = engine.get("spec") or {}
        name = str(engine.get("displayName") or engine["name"].rsplit("/", 1)[-1])
        spec = AgentSpec()
        extension: dict[str, Any] = {}
        bindings: dict[str, Any] = {"resource_name": engine["name"]}
        spec.display_name = Value[str].declared(name, "displayName")
        if engine.get("description"):
            spec.description = Value[str].declared(redact(str(engine["description"]))[0], "description")
        spec.model.requested = Value[str].unknown("set in the agent's code")
        spec.instructions = Instructions(basis="set in the agent's code")

        identity_type = spec_data.get("identityType")
        account = spec_data.get("serviceAccount")
        if identity_type == "AGENT_IDENTITY":
            spec.permissions.identity = Value[IdentityKind].declared(IdentityKind.SERVICE_PRINCIPAL, "spec.identityType AGENT_IDENTITY")
            if engine.get("effectiveIdentity") or spec_data.get("effectiveIdentity"):
                spec.permissions.principal = Value[str].declared(
                    str(engine.get("effectiveIdentity") or spec_data.get("effectiveIdentity")), "effectiveIdentity"
                )
            else:
                spec.permissions.principal = Value[str].unknown("the agent identity is issued at deploy time")
        else:
            spec.permissions.identity = Value[IdentityKind].derived(
                IdentityKind.SERVICE_PRINCIPAL, "Agent Engine agents run as a service account"
            )
            spec.permissions.principal = (
                Value[str].declared(str(account), "spec.serviceAccount") if account
                else Value[str].derived("the project's Reasoning Engine Service Agent", "no service account is set")
            )

        if spec_data.get("agentFramework"):
            extension["framework"] = spec_data["agentFramework"]
        methods = [m.get("name") for m in spec_data.get("classMethods") or [] if isinstance(m, dict) and m.get("name")]
        if methods:
            extension["class_methods"] = sorted(methods)
        deployment = spec_data.get("deploymentSpec") or {}
        env = sorted(v.get("name") for v in deployment.get("env") or [] if isinstance(v, dict) and v.get("name"))
        if env:
            extension["environment_variables"] = env
        secrets = [
            {"name": v.get("name"), "secret": (v.get("secretRef") or {}).get("secret")}
            for v in deployment.get("secretEnv") or [] if isinstance(v, dict)
        ]
        if secrets:
            extension["secret_environment_variables"] = secrets
        if deployment.get("resourceLimits"):
            extension["resource_limits"] = deployment["resourceLimits"]

        source = spec_data.get("sourceCodeSpec") or {}
        connect = ((source.get("developerConnectSource") or {}).get("config")) or {}
        if connect:
            extension["source"] = {k: connect[k] for k in ("gitRepositoryLink", "dir", "revision") if k in connect}
        python = source.get("pythonSpec") or {}
        if python:
            extension["entrypoint"] = {k: python[k] for k in ("entrypointModule", "entrypointObject", "version") if k in python}
        if (spec_data.get("containerSpec") or {}).get("imageUri"):
            extension["container_image"] = spec_data["containerSpec"]["imageUri"]
        package = spec_data.get("packageSpec") or {}
        if package:
            bindings["package"] = {k: v for k, v in package.items()}
        if engine.get("labels"):
            extension["labels"] = engine["labels"]
        for key in ("createTime", "updateTime"):
            if engine.get(key):
                bindings[key] = engine[key]
        spec.extensions[FORMAT] = extension
        return build_record(
            agent=f"{FORMAT}/{slug(name)}",
            platform=FORMAT,
            source=Source(format=FORMAT, artifact=artifact, locator=engine["name"].rsplit("/", 1)[-1],
                          detected_by="Agent Engine reasoningEngine resource",
                          commit=options.commit, pipeline_run=options.pipeline_run),
            spec=spec,
            bindings=bindings,
        )
