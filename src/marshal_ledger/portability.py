"""Portability: what it would take to move an agent to another platform.

A portability report has two parts, kept separate:

1. **Platform limitations**, from the target's capability matrix
   (``src/marshal_ledger/capabilities/``): tool kinds, knowledge sources, identity
   modes and model providers the target supports natively, only after rework,
   or not at all.
2. **Agent-specific issues**, from actually converting the agent with the
   target's writer, where Marshal has one: fields of this agent that don't
   carry over for reasons the matrix doesn't cover.

The score weighs each element of the agent (model, identity, instructions,
each tool, each knowledge source) by how much it matters for governance, and
deducts for each limitation or issue. Unknown values in the source are listed
but don't count against the score.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, Optional

import yaml

from .fidelity import FidelityReport, Outcome, matches
from .model import AgentVersion

LEVELS = ("native", "adapt", "none")
_PENALTY = {"native": 0.0, "adapt": 0.5, "none": 1.0}
_AGENT_PENALTY = 0.25
_WEIGHTS = {"model": 2.0, "identity": 3.0, "instructions": 2.0, "tool": 3.0, "knowledge": 2.0, "confirmation": 1.0}


@dataclass(frozen=True)
class Rule:
    pattern: str
    level: str
    label: str
    advice: Optional[str] = None


@dataclass
class Matrix:
    platform: str
    display_name: str
    writer: Optional[str]
    tools: list[Rule]
    knowledge: list[Rule]
    identity: list[Rule]
    models: list[Rule]
    confirmation: str = "native"
    sources: list[str] = field(default_factory=list)

    def match(self, rules: list[Rule], value: str) -> Rule:
        for rule in rules:
            if matches(value, rule.pattern):
                return rule
        return Rule("*", "none", "unsupported")


def _rules(items: Any) -> list[Rule]:
    return [Rule(str(i["pattern"]), str(i["level"]), str(i.get("label", "")), i.get("advice")) for i in items or []]


def platforms() -> list[str]:
    files = resources.files("marshal_ledger") / "capabilities"
    return sorted(f.name.removesuffix(".yaml") for f in files.iterdir() if f.name.endswith(".yaml"))


def load_matrix(platform: str) -> Matrix:
    path = resources.files("marshal_ledger") / "capabilities" / f"{platform}.yaml"
    if not path.is_file():
        raise ValueError(f"no capability matrix for {platform!r}; platforms: {', '.join(platforms())}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return Matrix(
        platform=data["platform"],
        display_name=data.get("display_name", data["platform"]),
        writer=data.get("writer"),
        tools=_rules(data.get("tools")),
        knowledge=_rules(data.get("knowledge")),
        identity=_rules(data.get("identity")),
        models=_rules(data.get("models")),
        confirmation=str(data.get("confirmation", "native")),
        sources=list(data.get("sources") or []),
    )


@dataclass(frozen=True)
class Issue:
    kind: str
    """``limitation`` (from the capability matrix) or ``agent`` (from converting this agent)."""
    element: str
    level: str
    message: str
    advice: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class PortabilityReport:
    agent: str
    target: str
    target_name: str
    score: int
    limitations: list[Issue] = field(default_factory=list)
    agent_issues: list[Issue] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    fidelity: Optional[FidelityReport] = None

    def text(self) -> str:
        lines = [f"{self.agent} → {self.target_name}", "", f"Portability: {self.score} / 100"]
        marks = {"none": "✗", "adapt": "~"}
        if self.limitations:
            lines.append(f"  Platform limitations ({self.target}):")
            for issue in self.limitations:
                line = f"    {marks.get(issue.level, '~')} {issue.message}"
                if issue.advice:
                    line += f"; {issue.advice}"
                lines.append(line)
        if self.agent_issues or self.unknowns:
            lines.append("  Agent-specific issues:")
            for issue in self.agent_issues:
                lines.append(f"    ~ {issue.message}")
            for unknown in self.unknowns:
                lines.append(f"    ? {unknown}")
        if self.fidelity is not None:
            lines.append(f"Fidelity: {self.fidelity.summary()}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "target": self.target,
            "score": self.score,
            "limitations": [i.to_dict() for i in self.limitations],
            "agent_issues": [i.to_dict() for i in self.agent_issues],
            "unknowns": self.unknowns,
            **({"fidelity": self.fidelity.to_dict()["summary"]} if self.fidelity else {}),
        }


def _element(path: str) -> Optional[str]:
    """The portability element a fidelity path belongs to, or None for metadata and extensions."""
    match = re.match(r"^(tools|knowledge)\[(.+?)\]", path)
    if match:
        return f"{'tool' if match.group(1) == 'tools' else 'knowledge'}:{match.group(2)}"
    if path.startswith("model.requested") or path.startswith("model.provider"):
        return "model"
    if path in ("permissions.identity",):
        return "identity"
    if path == "instructions":
        return "instructions"
    return None


def assess(record: AgentVersion, target: str) -> PortabilityReport:
    """How portable ``record`` is to the platform ``target``."""
    matrix = load_matrix(target)
    spec = record.spec
    weights: dict[str, float] = {}
    penalties: dict[str, float] = {}
    report = PortabilityReport(agent=record.agent, target=matrix.platform, target_name=matrix.display_name, score=100)

    def limit(element: str, kind: str, rule: Rule, subject: str) -> None:
        weights[element] = _WEIGHTS[kind]
        penalties[element] = _PENALTY[rule.level]
        if rule.level != "native":
            report.limitations.append(Issue("limitation", element, rule.level, f"{subject}: {rule.label}", rule.advice))

    # Model, identity, instructions
    if spec.model.provider.is_known:
        limit("model", "model", matrix.match(matrix.models, str(spec.model.provider.value)),
              f"model {spec.model.requested.value or ''} ({spec.model.provider.value})".replace(" ()", ""))
    else:
        weights["model"] = _WEIGHTS["model"]
        penalties["model"] = 0.0
        report.unknowns.append("model provider unknown in the source")
    if spec.permissions.identity.is_known:
        limit("identity", "identity", matrix.match(matrix.identity, spec.permissions.identity.value.value),
              f"identity {spec.permissions.identity.value.value}")
    else:
        report.unknowns.append("identity unknown in the source")
    if spec.instructions.hash:
        weights["instructions"] = _WEIGHTS["instructions"]
        penalties["instructions"] = 0.0

    for tool in spec.tools:
        element = f"tool:{tool.ref}"
        limit(element, "tool", matrix.match(matrix.tools, tool.ref), f"tool {tool.name or tool.ref}")
        if tool.requires_confirmation.value is True and matrix.confirmation != "native":
            weights[f"confirmation:{tool.ref}"] = _WEIGHTS["confirmation"]
            penalties[f"confirmation:{tool.ref}"] = _PENALTY[matrix.confirmation]
            report.limitations.append(Issue("limitation", f"confirmation:{tool.ref}", matrix.confirmation,
                                            f"tool {tool.name or tool.ref}: human confirmation is implemented in code"))
        if not tool.scope.is_known:
            report.unknowns.append(f"tool {tool.name or tool.ref}: scope unknown in the source")
    for item in spec.knowledge:
        limit(f"knowledge:{item.ref}", "knowledge", matrix.match(matrix.knowledge, item.ref), f"knowledge {item.name or item.ref}")

    # Agent-specific issues, from converting this agent with the target's writer.
    if matrix.writer:
        from .convert import convert

        conversion = convert(record, matrix.writer)
        report.fidelity = conversion.report
        flagged: set[str] = set()
        for item in conversion.report.lossy:
            element = _element(item.path)
            if element is None or element not in weights or penalties.get(element, 0) > 0 or element in flagged:
                continue
            if item.outcome is Outcome.DEFAULTED and element in ("model", "identity"):
                continue
            flagged.add(element)
            penalties[element] = _AGENT_PENALTY
            subject = element.split(":", 1)[-1] if ":" in element else element
            report.agent_issues.append(Issue("agent", element, "adapt",
                                             f"{subject}: {item.path.split('].')[-1] if '].' in item.path else item.path} "
                                             f"{item.outcome.value}" + (f" ({item.note})" if item.note else "")))

    total = sum(weights.values())
    lost = sum(weights[e] * penalties.get(e, 0) for e in weights)
    report.score = round(100 * (1 - lost / total)) if total else 100
    return report
