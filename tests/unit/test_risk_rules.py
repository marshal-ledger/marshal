"""Organization risk rules: added to the built-in ratings, set on the server only."""

import pytest
from pydantic import ValidationError

from marshal_ledger.diff import diff_specs
from marshal_ledger.hashing import sha256_text
from marshal_ledger.model import (
    AgentSpec,
    Grant,
    IdentityKind,
    Instructions,
    Scope,
    Source,
    ToolUse,
    Value,
)
from marshal_ledger.readers.base import build_record
from marshal_ledger.report import markdown, text
from marshal_ledger.risk import RankedChange, RiskRules, Severity, load_rules, rank
from marshal_ledger.service import AgentReport


def version(spec, agent="finance/invoice", fmt="test"):
    return build_record(agent=agent, platform="test", source=Source(format=fmt, artifact="a.yml"), spec=spec)


def ranked(before, after, rules):
    a, b = version(before), version(after)
    return {r.change.path: r for r in rank(diff_specs(a.spec, b.spec), RiskRules.model_validate(rules), before=a, after=b)}


def model(requested=None, provider=None):
    out = {}
    if requested:
        out["requested"] = Value[str].declared(requested)
    if provider:
        out["provider"] = Value[str].declared(provider)
    return out


def test_no_rules_keeps_the_built_in_ratings():
    a, b = AgentSpec(model=model("gpt-5")), AgentSpec(model=model("gpt-5.1"))
    [plain] = rank(diff_specs(a, b))
    [same] = rank(diff_specs(a, b), RiskRules(), before=version(a), after=version(b))
    assert plain == same
    assert plain.severity is Severity.MEDIUM and plain.rule is None and plain.note == ""
    assert "rule" not in plain.to_dict()


def test_raise_names_the_rule_on_the_line():
    found = ranked(
        AgentSpec(), AgentSpec(description=Value[str].declared("Pays invoices.")),
        {"rules": [{"name": "descriptions", "match": {"paths": ["description"]}, "raise_to": "MED"}]},
    )
    line = found["description"]
    assert (line.severity, line.builtin, line.rule) == (Severity.MEDIUM, Severity.LOW, "descriptions")
    assert line.line() == "MED   ~ description: unknown → Pays invoices.  [raised from LOW by org rule descriptions]"
    assert line.to_dict()["builtin_severity"] == "LOW"
    assert RankedChange.from_dict(line.to_dict()) == line


def test_lower_shows_the_reason():
    rules = {"rules": [{
        "name": "routine-models", "match": {"paths": ["model.requested"]}, "lower_to": "LOW",
        "reason": "model changes are routine here",
    }]}
    found = ranked(AgentSpec(model=model("gpt-5")), AgentSpec(model=model("gpt-5.1")), rules)
    line = found["model.requested"]
    assert line.severity is Severity.LOW
    assert line.note == "[lowered from MED by org rule routine-models: model changes are routine here]"
    assert line.to_dict()["reason"] == "model changes are routine here"


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_lowering_without_a_reason_is_rejected_at_load(reason):
    rule = {"name": "quiet", "match": {"paths": ["model.*"]}, "lower_to": "LOW"}
    if reason is not None:
        rule["reason"] = reason
    with pytest.raises(ValidationError, match="needs a reason"):
        RiskRules.model_validate({"rules": [rule]})


@pytest.mark.parametrize("rules, message", [
    ({"rules": [{"name": "x", "match": {"paths": ["a"]}, "raise_to": "HIGH", "severity": "HIGH"}]}, "Extra inputs"),
    ({"rule": []}, "Extra inputs"),
    ({"rules": [{"name": "x", "match": {"path": ["a"]}, "raise_to": "HIGH"}]}, "Extra inputs"),
    ({"rules": [{"name": "x", "match": {"paths": ["a"]}}]}, "exactly one of raise_to or lower_to"),
    ({"rules": [{"name": "x", "match": {"paths": ["a"]}, "raise_to": "HIGH", "lower_to": "LOW", "reason": "r"}]},
     "exactly one"),
    ({"rules": [{"name": "x", "match": {"paths": ["a"]}, "raise_to": "CRITICAL"}]}, "LOW, MED or HIGH"),
    ({"rules": [{"name": "x", "match": {"paths": ["a"]}, "raise_to": "LOW"}]}, "raises nothing"),
    ({"rules": [{"name": "x", "match": {"paths": ["a"]}, "lower_to": "HIGH", "reason": "r"}]}, "lowers nothing"),
    ({"rules": [{"name": "x", "match": {}, "raise_to": "HIGH"}]}, "at least one condition"),
    ({"rules": [{"name": "x", "match": {"categories": ["tool"]}, "raise_to": "HIGH"}]}, "unknown categories"),
    ({"rules": [{"name": "x", "match": {"kinds": ["edited"]}, "raise_to": "HIGH"}]}, "unknown kinds"),
    ({"rules": [{"name": "x", "match": {"identity": ["root"]}, "raise_to": "HIGH"}]}, "identity"),
    ({"rules": [{"name": "x", "match": {"targets": ["payments.acme.example"]}, "raise_to": "HIGH"}]}, "not a target"),
    ({"rules": [{"name": "x", "match": {"paths": ["a"]}, "raise_to": "HIGH"}] * 2}, "unique"),
    ({"rules": [{"name": "approved_models", "match": {"paths": ["a"]}, "raise_to": "HIGH"}]}, "reserved"),
    ({"approved_models": {}}, "providers, models or both"),
    ({"approved_models": {"provider": ["anthropic"]}}, "Extra inputs"),
])
def test_bad_rules_are_errors(rules, message):
    with pytest.raises(ValidationError, match=message):
        RiskRules.model_validate(rules)


def test_a_rule_that_matches_nothing_is_not_an_error():
    found = ranked(AgentSpec(), AgentSpec(description=Value[str].declared("d")),
                   {"rules": [{"name": "unused", "match": {"refs": ["tool/never-used"]}, "raise_to": "HIGH"}]})
    assert found["description"].severity is Severity.LOW and found["description"].rule is None


def test_approved_models():
    rules = {"approved_models": {"providers": ["anthropic", "amazon"], "models": ["claude-*", "anthropic.claude-*"]}}
    found = ranked(AgentSpec(model=model("claude-sonnet-4", "anthropic")),
                   AgentSpec(model=model("gpt-5", "openai")), rules)
    assert found["model.requested"].severity is Severity.HIGH
    assert found["model.requested"].note == "[raised from MED by org rule approved_models: gpt-5 is not an approved model]"
    # A new provider is already HIGH; the line keeps its built-in rating and no note.
    assert found["model.provider"].severity is Severity.HIGH and found["model.provider"].rule is None

    approved = ranked(AgentSpec(model=model("claude-sonnet-4", "anthropic")),
                      AgentSpec(model=model("Claude-Opus-5", "anthropic")), rules)
    assert approved["model.requested"].severity is Severity.MEDIUM and approved["model.requested"].rule is None


def test_approved_models_with_a_chosen_level_and_unknown_models():
    rules = {"approved_models": {"models": ["claude-*"], "raise_to": "MED"}}
    found = ranked(AgentSpec(model=model("claude-sonnet-4")), AgentSpec(), rules)
    # The model became unknown: not on the list, and MED is already its built-in rating.
    assert found["model.requested"].severity is Severity.MEDIUM
    assert ranked(AgentSpec(), AgentSpec(model=model("claude-x")), rules)["model.requested"].rule is None


def test_sensitive_tools_by_ref_glob():
    rules = {"rules": [{"name": "erp-writes", "match": {"refs": ["tool/sap.*"]}, "raise_to": "HIGH",
                        "reason": "the ERP is the system of record"}]}
    found = ranked(AgentSpec(), AgentSpec(tools=[ToolUse(ref="tool/sap.read_po", scope=Value[Scope].declared(Scope.READ)),
                                                 ToolUse(ref="tool/crm.read", scope=Value[Scope].declared(Scope.READ))]), rules)
    assert found["tool tool/sap.read_po"].severity is Severity.HIGH
    assert found["tool tool/sap.read_po"].note == "[raised from LOW by org rule erp-writes: the ERP is the system of record]"
    assert found["tool tool/crm.read"].severity is Severity.LOW


def read_tool(ref, **kwargs):
    return ToolUse(ref=ref, scope=Value[Scope].declared(Scope.READ), reversible=Value[bool].declared(True), **kwargs)


PAYMENTS = {"rules": [{"name": "payments", "match": {"targets": ["host:Payments.acme.example"]}, "raise_to": "HIGH"}]}


@pytest.mark.parametrize("ref", [
    "tool/http.POST.https://payments.acme.example/refunds/{id}",
    "tool/mcp.https://payments.acme.example/mcp",
])
def test_hosts_match_through_resolved_targets_whenever_added_removed_or_changed(ref):
    tool = read_tool(ref)
    assert ranked(AgentSpec(), AgentSpec(tools=[tool]), PAYMENTS)[f"tool {ref}"].severity is Severity.HIGH
    assert ranked(AgentSpec(tools=[tool]), AgentSpec(), PAYMENTS)[f"tool {ref}"].severity is Severity.HIGH
    changed = ranked(AgentSpec(tools=[tool]), AgentSpec(tools=[read_tool(ref, description="Refunds.")]), PAYMENTS)
    assert changed[f"tool {ref} description"].rule == "payments"
    other = "tool/http.POST.https://shop.acme.example/refunds"
    assert ranked(AgentSpec(), AgentSpec(tools=[read_tool(other)]), PAYMENTS)[f"tool {other}"].rule is None


def test_network_grants_and_target_globs():
    rules = {"rules": [{"name": "acme-hosts", "match": {"targets": ["host:*.acme.example"]}, "raise_to": "HIGH"}]}
    grant = Grant(target="other/network.ledger.acme.example", privilege="CONNECT", scope=Scope.EXECUTE)
    found = ranked(AgentSpec(), AgentSpec(permissions={"grants": [grant]}), rules)
    assert found["grant other/network.ledger.acme.example CONNECT"].severity is Severity.HIGH


def test_only_raising_rules_follow_links_a_person_attested():
    """An agent team writes its own Marshal file links, so a link can raise a rating but never lower one."""
    ref = "tool/python.app/tools.py:refund"
    links = {"marshal-file": {"links": [{"ref": ref, "same_as": "host:payments.acme.example"}]}}
    after = version(AgentSpec(tools=[read_tool(ref)], extensions=links), fmt="marshal-file")
    before = version(AgentSpec(extensions=links), fmt="marshal-file")
    changes = [c for c in diff_specs(before.spec, after.spec) if c.path == f"tool {ref}"]
    raised = rank(changes, RiskRules.model_validate(PAYMENTS), before=before, after=after)
    assert raised[0].rule == "payments"
    lowering = {"rules": [{"name": "sandbox", "match": {"targets": ["host:payments.acme.example"]}, "lower_to": "LOW",
                           "reason": "sandbox"}]}
    write = ToolUse(ref=ref, scope=Value[Scope].declared(Scope.WRITE))
    after = version(AgentSpec(tools=[write], extensions=links), fmt="marshal-file")
    changes = [c for c in diff_specs(before.spec, after.spec) if c.path == f"tool {ref}"]
    [kept] = rank(changes, RiskRules.model_validate(lowering), before=before, after=after)
    assert kept.severity is Severity.HIGH and kept.rule is None


def test_identity_modes():
    rules = {"rules": [{"name": "shared-credential-tools", "match": {"identity": ["shared_credential"],
                                                                    "categories": ["tools"]}, "raise_to": "HIGH"}]}
    shared = {"identity": Value[IdentityKind].declared(IdentityKind.SHARED_CREDENTIAL)}
    found = ranked(AgentSpec(permissions=shared), AgentSpec(permissions=shared, tools=[read_tool("tool/lookup")]), rules)
    assert found["tool tool/lookup"].severity is Severity.HIGH
    own = {"identity": Value[IdentityKind].declared(IdentityKind.ON_BEHALF_OF_USER)}
    found = ranked(AgentSpec(permissions=own), AgentSpec(permissions=own, tools=[read_tool("tool/lookup")]), rules)
    assert found["tool tool/lookup"].severity is Severity.LOW


def test_field_path_globs():
    rules = {"rules": [{"name": "connections", "match": {"paths": ["extensions.copilot-studio.*.connection_mode"]},
                        "lower_to": "MED", "reason": "connections are reviewed in Power Platform"}]}
    a = AgentSpec(extensions={"copilot-studio": {"actions": {"x": {"connection_mode": "Invoker"}}}})
    b = AgentSpec(extensions={"copilot-studio": {"actions": {"x": {"connection_mode": "Maker"}}}})
    line = ranked(a, b, rules)["extensions.copilot-studio.actions.x.connection_mode"]
    assert (line.builtin, line.severity) == (Severity.HIGH, Severity.MEDIUM)
    assert "connections are reviewed in Power Platform" in line.line()


def instructions(text):
    return Instructions(hash=sha256_text(text), text=text)


def test_a_removed_guardrail_is_never_lowered():
    rules = {"rules": [{"name": "instructions", "match": {"categories": ["instructions"]}, "lower_to": "LOW",
                        "reason": "prompt edits are routine"}]}
    a = AgentSpec(instructions=instructions("Refund orders.\nNever refund more than $500."))
    b = AgentSpec(instructions=instructions("Refund orders."))
    assert ranked(a, b, rules)["instructions"].severity is Severity.HIGH
    c = AgentSpec(instructions=instructions("Refund orders politely."))
    assert ranked(a, c, rules)["instructions"].severity is Severity.HIGH  # the guardrail line went too
    assert ranked(b, c, rules)["instructions"].severity is Severity.LOW


def test_a_raise_wins_over_a_lower_and_the_first_rule_names_ties():
    rules = {"rules": [
        {"name": "quiet-models", "match": {"categories": ["model"]}, "lower_to": "LOW", "reason": "routine"},
        {"name": "watch-a", "match": {"paths": ["model.requested"]}, "raise_to": "HIGH"},
        {"name": "watch-b", "match": {"paths": ["model.*"]}, "raise_to": "HIGH"},
    ]}
    found = ranked(AgentSpec(model=model("a")), AgentSpec(model=model("b")), rules)
    assert (found["model.requested"].severity, found["model.requested"].rule) == (Severity.HIGH, "watch-a")


def test_ordering_stays_stable_after_rules():
    rules = {"rules": [{"name": "descriptions", "match": {"paths": ["description"]}, "raise_to": "HIGH"}]}
    a, b = AgentSpec(), AgentSpec(description=Value[str].declared("d"), tools=[read_tool("tool/z"), read_tool("tool/a")])
    order = [r.change.path for r in rank(diff_specs(a, b), RiskRules.model_validate(rules), after=version(b))]
    assert order == ["description", "tool tool/a", "tool tool/z"]


def test_load_rules_reads_a_server_configuration_or_the_rules_alone():
    alone = {"rules": [{"name": "x", "match": {"paths": ["a"]}, "raise_to": "HIGH"}]}
    assert load_rules(alone).rules[0].name == "x"
    assert load_rules({"store": "x", "issuers": [], "policy": {"risk": alone}}).rules[0].name == "x"
    assert load_rules({"store": "x", "issuers": []}).empty
    assert load_rules(None).empty


def test_reports_show_the_rule_in_text_and_markdown():
    line = RankedChange.from_dict({"severity": "LOW", "builtin_severity": "MED", "rule": "routine-models",
                                   "reason": "model changes are routine here", "category": "model",
                                   "kind": "changed", "path": "model.requested", "before": "a", "after": "b"})
    report = AgentReport(agent="finance/invoice", platform="test", event="proposed", environment="prod",
                         version="sha256:x", label="v2", new_version=True, baseline="v1", completeness=0.5,
                         changes=[line])
    expected = "LOW   ~ model.requested: a → b  [lowered from MED by org rule routine-models: model changes are routine here]"
    assert expected in text([report])
    assert expected in markdown([report])
    assert AgentReport.from_dict(report.to_dict()) == report


def test_clients_read_reports_with_keys_they_do_not_know():
    data = {"agent": "a", "platform": "p", "event": "proposed", "environment": "prod", "version": "v", "label": "v1",
            "new_version": True, "baseline": None, "completeness": 1.0, "a_later_field": 1,
            "changes": [{"severity": "MED", "category": "model", "kind": "changed", "path": "model.requested",
                         "a_later_key": "x"}]}
    assert AgentReport.from_dict(data).changes[0].severity is Severity.MEDIUM
