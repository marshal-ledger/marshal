"""Risk ranking for agent changes.

Each change from :mod:`marshal_ledger.diff` gets a severity. The rules put the
changes a reviewer most needs to see first: escalated permissions, new tools
that can write, a new model provider, instructions that lost a guardrail, and
removed knowledge. An unknown scope is ranked as if it were a write, because the
ledger does not assume a tool is safe.

These built-in rules are the floor. An organization adds its own in the ingest
server's configuration (:class:`RiskRules`, ``policy.risk``): approved models,
sensitive tools, knowledge sources and hosts, identity modes and field paths.
Raising a rating is free; lowering one needs a reason, and every line a rule
changed names the rule. A removed guardrail is never lowered. Rules are read by
the server only, so an agent team can't change how its own changes are rated.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import IntEnum
from typing import Annotated, Any, Optional

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

from .diff import CATEGORIES, Change, describe
from .fidelity import matches as glob
from .model import AgentVersion, IdentityKind
from .resolve import TARGET_KINDS, links, normalize_target, targets

_SCOPE_RANK = {"read": 1, "execute": 2, "write": 3, "unknown": 3, "admin": 4}


class Severity(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3

    @property
    def label(self) -> str:
        return {Severity.LOW: "LOW", Severity.MEDIUM: "MED", Severity.HIGH: "HIGH"}[self]


_LABELS = {s.label: s for s in Severity}
_CHANGE_FIELDS = {f.name for f in fields(Change)}


@dataclass(frozen=True)
class RankedChange:
    severity: Severity
    change: Change
    builtin: Optional[Severity] = None
    """When an organization rule changed the rating: what the built-in rules gave."""
    rule: Optional[str] = None
    """The organization rule that set :attr:`severity`."""
    reason: Optional[str] = None
    """Why, as the rule states it. Always present when the rule lowered the rating."""

    def to_dict(self) -> dict[str, Any]:
        data = {"severity": self.severity.label, **self.change.to_dict()}
        if self.rule is not None:
            data["builtin_severity"] = self.builtin.label if self.builtin else None
            data["rule"] = self.rule
            if self.reason:
                data["reason"] = self.reason
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RankedChange":
        # Unknown keys are skipped, so a client can read a newer server's reports.
        builtin = data.get("builtin_severity")
        return cls(
            _LABELS[data["severity"]],
            Change(**{k: v for k, v in data.items() if k in _CHANGE_FIELDS}),
            _LABELS.get(builtin) if builtin else None,
            data.get("rule"),
            data.get("reason"),
        )

    @property
    def note(self) -> str:
        """Which organization rule set the rating, and why; empty for a built-in rating."""
        if self.rule is None or self.builtin is None:
            return ""
        verb = "raised" if self.severity > self.builtin else "lowered"
        return f"[{verb} from {self.builtin.label} by org rule {self.rule}" + (f": {self.reason}]" if self.reason else "]")

    def line(self) -> str:
        return f"{self.severity.label:<4}  {describe(self.change)}" + (f"  {self.note}" if self.note else "")


def _rank(scope: Optional[str]) -> int:
    return _SCOPE_RANK.get(str(scope), _SCOPE_RANK["unknown"])


# Platform-specific fields that control who the agent acts as, who may use it, or what checks its actions.
SENSITIVE_EXTENSION_KEYS: dict[str, Severity] = {
    "connection_mode": Severity.HIGH,
    "credentials": Severity.HIGH,
    "authenticationMode": Severity.HIGH,
    "accessControlPolicy": Severity.HIGH,
    "default_agent_user": Severity.HIGH,
    "user_api_scopes": Severity.HIGH,
    "authorizer": Severity.HIGH,
    "managed_policies": Severity.HIGH,
    "gateway_auth": Severity.HIGH,
    "mcp_headers": Severity.HIGH,
    "hooks": Severity.HIGH,
    "allowed_tools": Severity.MEDIUM,
    "guardrail": Severity.MEDIUM,
    "security": Severity.MEDIUM,
    "permissions": Severity.MEDIUM,
}


def severity(change: Change) -> Severity:
    path, kind = change.path, change.kind

    if change.category == "extensions":
        parts = set(path.split("."))
        found = [level for key, level in SENSITIVE_EXTENSION_KEYS.items() if key in parts]
        return max(found) if found else Severity.LOW

    if change.category == "permissions":
        if path == "permissions.identity":
            return Severity.HIGH
        if path == "permissions.principal":
            return Severity.MEDIUM
        if path.startswith("grant "):
            if kind == "added":
                return Severity.HIGH if _rank(change.scope) >= _rank("write") else Severity.MEDIUM
            return Severity.LOW
        if path.startswith("access "):
            return Severity.MEDIUM if kind == "added" else Severity.LOW

    if change.category == "tools":
        if kind == "added":
            if _rank(change.scope) >= _rank("write") or change.reversible == "false":
                return Severity.HIGH
            return Severity.MEDIUM if _rank(change.scope) == _rank("execute") else Severity.LOW
        if kind == "removed":
            return Severity.LOW
        if path.endswith(" scope"):
            return Severity.HIGH if _rank(change.after) > _rank(change.before) else Severity.LOW
        if path.endswith(" reversible"):
            after = str(change.after).lower()
            if after == "false":
                return Severity.HIGH
            return Severity.MEDIUM if after == "unknown" else Severity.LOW
        if path.endswith(" requires_confirmation"):
            # Losing a human approval step is an escalation.
            return Severity.HIGH if str(change.before).lower() == "true" else Severity.LOW
        return Severity.LOW

    if change.category == "model":
        # A routine upgrade within one provider is reviewed, not escalated; a new provider
        # means a new party sees the agent's data.
        return Severity.HIGH if path == "model.provider" else Severity.MEDIUM

    if change.category == "instructions":
        # Instructions often carry the agent's guardrails, so any change is reviewed.
        return Severity.HIGH if change.guardrails else Severity.MEDIUM

    if change.category == "knowledge":
        if kind == "removed":
            return Severity.MEDIUM
        if kind == "added":
            if change.scope == "unknown":
                return Severity.MEDIUM
            return Severity.HIGH if _rank(change.scope) >= _rank("write") else Severity.LOW
        if _rank(change.after) > _rank(change.before):
            return Severity.HIGH if change.after == "write" else Severity.MEDIUM
        return Severity.LOW

    return Severity.LOW


def rank(
    changes: list[Change],
    rules: Optional["RiskRules"] = None,
    *,
    before: Optional[AgentVersion] = None,
    after: Optional[AgentVersion] = None,
) -> list[RankedChange]:
    """Changes sorted by severity, highest first. Ties keep their diff order.

    With ``rules``, the organization's rules adjust the built-in ratings. ``before`` and
    ``after`` are the versions compared; rules that match targets or identity need them.
    """
    ranked = [RankedChange(severity(c), c) for c in changes]
    if rules is not None and not rules.empty:
        context = _Context.of(before, after)
        ranked = [rules.apply(r, context) for r in ranked]
    return sorted(ranked, key=lambda r: -r.severity)


# Organization rules


def _level(value: Any) -> Any:
    if isinstance(value, str):
        found = {"LOW": Severity.LOW, "MED": Severity.MEDIUM, "MEDIUM": Severity.MEDIUM, "HIGH": Severity.HIGH}
        if value.strip().upper() in found:
            return found[value.strip().upper()]
    if isinstance(value, Severity):
        return value
    raise ValueError("a severity is LOW, MED or HIGH")


Level = Annotated[Severity, BeforeValidator(_level)]


def _ref(change: Change) -> Optional[str]:
    """The tool, knowledge source or grant target a change is about."""
    if change.category in ("tools", "knowledge") or change.path.startswith("grant "):
        parts = change.path.split(" ")
        return parts[1] if len(parts) > 1 else None
    return None


@dataclass(frozen=True)
class _Context:
    """What rules can match beyond the change itself: the agent's identity in either version,
    and the targets each ref reaches (resolve.targets) in either version."""

    identities: frozenset[str]
    stated: dict[str, frozenset[str]]
    """Targets the definition states, or a vendor documents."""
    attested: dict[str, frozenset[str]]
    """Targets only a person's link in a Marshal file states."""

    @classmethod
    def of(cls, *records: Optional[AgentVersion]) -> "_Context":
        identities: set[str] = set()
        stated: dict[str, set[str]] = {}
        attested: dict[str, set[str]] = {}
        for record in records:
            if record is None:
                continue
            identity = record.spec.permissions.identity
            if identity.is_known:
                identities.add(getattr(identity.value, "value", str(identity.value)))
            own: dict[str, list[tuple[str, str]]] = {}
            for ref, target in links(record):
                own.setdefault(ref, []).append((target, "attested"))
            for found in targets(record, own):
                (attested if found.attested else stated).setdefault(found.ref, set()).add(found.target)
        return cls(
            frozenset(identities),
            {k: frozenset(v) for k, v in stated.items()},
            {k: frozenset(v) for k, v in attested.items()},
        )


class RuleMatch(BaseModel):
    """Which changes a rule applies to. Every condition given must hold; within a list, any entry may."""

    model_config = ConfigDict(extra="forbid")

    categories: list[str] = Field(default_factory=list)
    """Change categories: permissions, tools, model, knowledge, instructions, metadata, extensions."""
    kinds: list[str] = Field(default_factory=list)
    """``added``, ``removed`` or ``changed``."""
    paths: list[str] = Field(default_factory=list)
    """Globs over the change's path, such as ``model.*`` or ``extensions.copilot-studio.*.connection_mode``."""
    refs: list[str] = Field(default_factory=list)
    """Globs over the ref of the tool, knowledge source or grant target changed, such as ``tool/http.*``."""
    targets: list[str] = Field(default_factory=list)
    """Targets the changed ref reaches, such as ``host:payments.acme.example`` or ``mcp:npm:*``."""
    identity: list[IdentityKind] = Field(default_factory=list)
    """The agent's identity in the version before or after the change, such as ``shared_credential``."""

    @field_validator("categories")
    @classmethod
    def _categories(cls, value: list[str]) -> list[str]:
        unknown = [c for c in value if c not in CATEGORIES]
        if unknown:
            raise ValueError(f"unknown categories {unknown}; use {', '.join(CATEGORIES)}")
        return value

    @field_validator("kinds")
    @classmethod
    def _kinds(cls, value: list[str]) -> list[str]:
        unknown = [k for k in value if k not in ("added", "removed", "changed")]
        if unknown:
            raise ValueError(f"unknown kinds {unknown}; use added, removed or changed")
        return value

    @field_validator("targets")
    @classmethod
    def _targets(cls, value: list[str]) -> list[str]:
        out = []
        for pattern in value:
            kind = pattern.partition(":")[0].lower()
            if kind not in TARGET_KINDS + ("ref",) or ":" not in pattern:
                raise ValueError(f"{pattern!r} is not a target; start it with one of {', '.join(TARGET_KINDS)}:")
            if "*" in pattern:
                out.append(pattern.lower() if kind == "host" else pattern)
                continue
            normalized = normalize_target(pattern) if kind != "ref" else pattern
            if normalized is None:
                raise ValueError(f"{pattern!r} is not a target Marshal can match")
            out.append(normalized)
        return out

    @model_validator(mode="after")
    def _something(self) -> "RuleMatch":
        if not (self.categories or self.kinds or self.paths or self.refs or self.targets or self.identity):
            raise ValueError("a rule needs at least one condition, such as paths, refs, targets or identity")
        return self

    def applies(self, change: Change, context: _Context, *, attested: bool) -> bool:
        if self.categories and change.category not in self.categories:
            return False
        if self.kinds and change.kind not in self.kinds:
            return False
        if self.paths and not any(glob(change.path, p) for p in self.paths):
            return False
        ref = _ref(change)
        if self.refs and not (ref and any(glob(ref, p) for p in self.refs)):
            return False
        if self.targets:
            reached = set(context.stated.get(ref, ())) if ref else set()
            if ref and attested:
                reached |= context.attested.get(ref, frozenset())
            if not any(glob(t, p) for t in sorted(reached) for p in self.targets):
                return False
        if self.identity and not context.identities & {i.value for i in self.identity}:
            return False
        return True


class RiskRule(BaseModel):
    """One organization rule: the changes it matches, and the rating it raises or lowers them to."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    """Shown on every line the rule changes."""
    match: RuleMatch
    raise_to: Optional[Level] = None
    """Matching changes are rated at least this."""
    lower_to: Optional[Level] = None
    """Matching changes are rated at most this. Needs a ``reason``; a removed guardrail is never lowered."""
    reason: Optional[str] = Field(default=None, max_length=200)
    """Why. Required to lower; shown beside the rating."""

    @model_validator(mode="after")
    def _direction(self) -> "RiskRule":
        if (self.raise_to is None) == (self.lower_to is None):
            raise ValueError(f"rule {self.name}: give exactly one of raise_to or lower_to")
        if self.raise_to is Severity.LOW:
            raise ValueError(f"rule {self.name}: raise_to LOW raises nothing")
        if self.lower_to is Severity.HIGH:
            raise ValueError(f"rule {self.name}: lower_to HIGH lowers nothing")
        if self.lower_to is not None and not (self.reason or "").strip():
            raise ValueError(f"rule {self.name}: lowering a rating needs a reason, which the report shows")
        return self


class ApprovedModels(BaseModel):
    """Models and providers the organization approves. A model change to anything else is raised."""

    model_config = ConfigDict(extra="forbid")

    providers: list[str] = Field(default_factory=list)
    """Globs over ``model.provider``, such as ``anthropic`` or ``amazon``. Case is ignored."""
    models: list[str] = Field(default_factory=list)
    """Globs over ``model.requested`` and ``model.resolved``, such as ``claude-*``. Case is ignored."""
    raise_to: Level = Severity.HIGH

    @model_validator(mode="after")
    def _something(self) -> "ApprovedModels":
        if not (self.providers or self.models):
            raise ValueError("approved_models needs providers, models or both")
        if self.raise_to is Severity.LOW:
            raise ValueError("approved_models: raise_to LOW raises nothing")
        return self

    def objection(self, change: Change) -> Optional[str]:
        """Why ``change`` isn't approved, or None."""
        if change.category != "model":
            return None
        if change.path == "model.provider" and self.providers:
            patterns, what = self.providers, "provider"
        elif change.path in ("model.requested", "model.resolved") and self.models:
            patterns, what = self.models, "model"
        else:
            return None
        value = str(change.after)
        if any(glob(value.lower(), p.lower()) for p in patterns):
            return None
        # An unknown value isn't on any list: the ledger doesn't assume a model is approved.
        return f"{value} is not an approved {what}"


APPROVED_MODELS_RULE = "approved_models"


class RiskRules(BaseModel):
    """An organization's risk rules, set in the ingest server's configuration (``policy.risk``)."""

    model_config = ConfigDict(extra="forbid")

    approved_models: Optional[ApprovedModels] = None
    rules: list[RiskRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> "RiskRules":
        names = [r.name for r in self.rules]
        repeated = sorted({n for n in names if names.count(n) > 1})
        if repeated:
            raise ValueError(f"rule names must be unique: {', '.join(repeated)}")
        if APPROVED_MODELS_RULE in names:
            raise ValueError(f"the rule name {APPROVED_MODELS_RULE} is reserved")
        return self

    @property
    def empty(self) -> bool:
        return self.approved_models is None and not self.rules

    def apply(self, ranked: RankedChange, context: _Context) -> RankedChange:
        """``ranked`` with the organization's rating. Lowering rules apply first, then raising
        rules, so a raise always wins over a lower."""
        change, builtin = ranked.change, ranked.severity
        # A removed guardrail stays at its built-in rating or above; the word list can't tell a
        # reworded limit from a dropped one, so a reviewer should see it at full weight.
        guardrail = change.category == "instructions" and bool(change.guardrails)
        lowers = [] if guardrail else [
            r for r in self.rules if r.lower_to is not None and r.match.applies(change, context, attested=False)
        ]
        raises: list[tuple[Severity, str, Optional[str]]] = [
            (r.raise_to, r.name, r.reason) for r in self.rules
            if r.raise_to is not None and r.match.applies(change, context, attested=True)
        ]
        if self.approved_models is not None:
            objection = self.approved_models.objection(change)
            if objection:
                raises.insert(0, (self.approved_models.raise_to, APPROVED_MODELS_RULE, objection))

        level = builtin
        lowest = min(lowers, key=lambda r: r.lower_to, default=None)  # first of the lowest, in file order
        if lowest is not None:
            level = min(level, lowest.lower_to)
        highest = max(raises, key=lambda r: r[0], default=None)  # first of the highest, in file order
        if highest is not None:
            level = max(level, highest[0])

        if level > builtin:
            return RankedChange(level, change, builtin, highest[1], highest[2])
        if level < builtin:
            return RankedChange(level, change, builtin, lowest.name, lowest.reason)
        return ranked


def load_rules(document: Any) -> RiskRules:
    """Risk rules from a parsed YAML document: either the rules themselves, or a server
    configuration whose ``policy.risk`` holds them."""
    if isinstance(document, dict) and ("policy" in document or "issuers" in document):
        document = (document.get("policy") or {}).get("risk") or {}
    return RiskRules.model_validate(document or {})
