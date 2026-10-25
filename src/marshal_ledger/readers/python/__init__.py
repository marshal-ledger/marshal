"""Reader for agents defined in Python code.

Reads agents built with the OpenAI Agents SDK, LangChain and LangGraph prebuilt
agents, Google ADK and CrewAI by static analysis: source files are parsed,
never imported or run. Each agent constructor call is one agent. Arguments that
can only be known at run time (environment variables, function parameters,
awaited values) are recorded as unknown with the reason.

Mapping rules are documented in ``docs/formats/python.md``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

import yaml

from ...hashing import sha256_text
from ...model import (
    AgentSpec,
    Entity,
    EntityKind,
    IdentityKind,
    Instructions,
    KnowledgeUse,
    Provenance,
    Source,
    Value,
)
from ...redact import redact
from ..base import ReadOptions, ReadResult, Reader, Skipped, build_record, jsonable, slug
from .frameworks import (
    ORCHESTRATION_HINTS,
    AgentDraft,
    CrewAIProfile,
    GoogleAdkProfile,
    LangChainProfile,
    OpenAIAgentsProfile,
)
from .static import Call, Evaluator, Project, Template, Unknown, callee_name, iter_calls

FORMAT = "python"
_FRAMEWORK_IMPORT = re.compile(
    r"^\s*(from|import)\s+(agents|langchain|langgraph|crewai|google\.adk)\b", re.MULTILINE
)
_SETUP_CALLS = {"agents.set_default_openai_client"}


def _has_framework_imports(path: Path, limit: int = 500) -> bool:
    files = [path] if path.is_file() else sorted(path.rglob("*.py"))[:limit]
    for file in files:
        if file.suffix != ".py" or any(p in (".venv", "venv", "node_modules", "site-packages") for p in file.parts):
            continue
        try:
            if _FRAMEWORK_IMPORT.search(file.read_text(encoding="utf-8", errors="ignore")[:200_000]):
                return True
        except OSError:
            continue
    return False


def _site(call: Call) -> tuple[str, int, int]:
    return call.module.rel, call.node.lineno, call.node.col_offset


def _crewai_configs(project: Project) -> dict[str, dict[str, Any]]:
    """``config/agents.yaml`` next to each module that uses CrewAI's project layout."""
    configs: dict[str, dict[str, Any]] = {}
    for module in project.unique_modules():
        config = module.path.parent / "config" / "agents.yaml"
        if config.is_file() and "agents_config" in module.path.read_text(encoding="utf-8", errors="ignore"):
            try:
                data = jsonable(yaml.safe_load(config.read_text(encoding="utf-8")))
            except yaml.YAMLError:
                continue
            if isinstance(data, dict):
                configs[module.rel] = data
    return configs


class PythonAgentReader(Reader):
    format = FORMAT
    platform = FORMAT

    @classmethod
    def detect(cls, path: Path) -> bool:
        return path.exists() and (path.is_dir() or path.suffix == ".py") and _has_framework_imports(path)

    def read(self, path: Path, options: ReadOptions | None = None) -> ReadResult:
        options = options or ReadOptions()
        root = path if path.is_dir() else path.parent
        project = Project(root)
        result = ReadResult(warnings=list(project.errors))
        evaluator = Evaluator(project)
        only = path.resolve() if path.is_file() else None

        calls: list[tuple[Call, Optional[str]]] = []
        setup: list[Call] = []
        constructors = {
            *OpenAIAgentsProfile.constructors, *LangChainProfile.constructors,
            *GoogleAdkProfile.constructors, *CrewAIProfile.constructors,
        }
        for node, scope, assigned in iter_calls(project):
            if only is not None and scope.module.path.resolve() != only:
                continue
            name = callee_name(evaluator.eval(node.func, scope))
            if name in constructors or name in _SETUP_CALLS or name in ORCHESTRATION_HINTS:
                value = evaluator.eval(node, scope)
                if not isinstance(value, Call):
                    continue
                if name in _SETUP_CALLS:
                    setup.append(value)
                else:
                    calls.append((value, assigned))

        setup_calls = setup
        profiles = [
            OpenAIAgentsProfile(evaluator, setup_calls),
            LangChainProfile(evaluator, setup_calls),
            GoogleAdkProfile(evaluator, setup_calls),
            CrewAIProfile(evaluator, setup_calls, _crewai_configs(project)),
        ]
        drafts: list[tuple[AgentDraft, Optional[str]]] = []
        for call, assigned in calls:
            if call.qualname in ORCHESTRATION_HINTS:
                label = call.kwargs.get("name") if isinstance(call.kwargs.get("name"), str) else assigned
                result.skipped.append(Skipped(call.module.rel, f"{call.qualname} {label or ''} at line {call.node.lineno}".replace("  ", " "),
                                              ORCHESTRATION_HINTS[call.qualname]))
                continue
            for profile in profiles:
                if profile.matches(call.qualname):
                    drafts.append((profile.draft(call), assigned))
                    break

        # Agent ids first, so agents can refer to each other.
        ids: dict[tuple[str, int, int], str] = {}
        used: dict[str, int] = {}
        for draft, assigned in drafts:
            base = slug(draft.name or assigned or f"{draft.call.module.name}-{draft.call.node.lineno}")
            agent_id = f"{root.name}/{base}"
            used[agent_id] = used.get(agent_id, 0) + 1
            if used[agent_id] > 1:
                agent_id = f"{agent_id}-{used[agent_id]}"
            ids[_site(draft.call)] = agent_id

        entities: dict[str, Entity] = {}
        for draft, _ in drafts:
            record = self._record(draft, ids, options, result, entities)
            result.records.append(record)
            result.warnings += draft.warnings
        result.entities = sorted(entities.values(), key=lambda e: e.ref)
        return result

    def _record(
        self,
        draft: AgentDraft,
        ids: dict[tuple[str, int, int], str],
        options: ReadOptions,
        result: ReadResult,
        entities: dict[str, Entity],
    ) -> Any:
        call = draft.call
        where = call.where
        agent_id = ids[_site(call)]
        spec = AgentSpec()
        if draft.name:
            spec.display_name = Value[str].declared(draft.name, where)
        if draft.description:
            spec.description = Value[str].declared(redact(draft.description)[0], where)
        requested = draft.model
        if requested.is_known and not requested.basis:
            requested = requested.model_copy(update={"basis": where})
        spec.model.requested = requested
        spec.model.provider = draft.provider
        spec.model.parameters = draft.parameters

        instructions = draft.instructions
        if isinstance(instructions, (str, Template)) and (not isinstance(instructions, str) or instructions):
            text = instructions if isinstance(instructions, str) else instructions.text()
            text, count = redact(text)
            if count:
                result.warnings.append(f"{where}: instructions: redacted {count} credential-like value(s)")
            if isinstance(instructions, Template):
                unresolved = ", ".join(sorted({u.reason for u in instructions.unresolved()}))
                provenance, basis = Provenance.DERIVED, f"{where}; unresolved parts in braces: {unresolved}"
            elif draft.instructions_rule:
                provenance, basis = Provenance.DERIVED, f"{where}; {draft.instructions_rule}"
            else:
                provenance, basis = Provenance.DECLARED, where
            spec.instructions = Instructions(
                hash=sha256_text(text), text=None if options.hash_only else text, provenance=provenance, basis=basis
            )
        elif isinstance(instructions, Unknown):
            spec.instructions = Instructions(basis=f"{where}: {instructions}")
        else:
            spec.instructions = Instructions(basis=f"{where}: no instructions argument")

        for tool in draft.tools:
            use = tool.use
            if tool.agent_call is not None:
                target = ids.get(_site(tool.agent_call))
                if target is None:
                    result.warnings.append(f"{where}: calls an agent that is not recorded ({tool.agent_call.qualname})")
                    continue
                use = use.model_copy(update={"ref": f"agent/{target}", "name": use.name or target.rsplit("/", 1)[-1]})
                entities.setdefault(use.ref, Entity(ref=use.ref, kind=EntityKind.AGENT, platform=draft.framework, identifier=target))
            else:
                entities.setdefault(use.ref, Entity(ref=use.ref, kind=EntityKind.TOOL, platform=draft.framework,
                                                    identifier=use.ref.split("/", 1)[1], display_name=use.name))
            if all(t.ref != use.ref for t in spec.tools):
                spec.tools.append(use)
        for ref, kind, name in draft.knowledge:
            if all(k.ref != ref for k in spec.knowledge):
                spec.knowledge.append(KnowledgeUse(ref=ref, type=kind, name=name))
                entities.setdefault(ref, Entity(ref=ref, kind=EntityKind.KNOWLEDGE, platform=draft.framework,
                                                identifier=ref.split("/", 1)[1], display_name=name))

        spec.permissions.identity = Value[IdentityKind].unknown("set by the runtime the agent is deployed to")
        spec.extensions[FORMAT] = {
            "framework": draft.framework,
            "constructor": call.qualname,
            "module": call.module.name,
            **draft.extension,
        }
        return build_record(
            agent=agent_id,
            platform=draft.framework,
            source=Source(
                format=FORMAT,
                artifact=call.module.rel,
                locator=f"{call.qualname} at line {call.node.lineno}",
                detected_by=f"{draft.framework} agent constructor",
                commit=options.commit,
                pipeline_run=options.pipeline_run,
            ),
            spec=spec,
        )


__all__ = ["PythonAgentReader"]
