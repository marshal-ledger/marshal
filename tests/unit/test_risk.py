from marshal_ledger.diff import diff_specs
from marshal_ledger.model import AgentSpec, Grant, IdentityKind, KnowledgeUse, Scope, ToolUse, Value
from marshal_ledger.risk import Severity, rank


def severities(a, b):
    return {r.change.path: r.severity for r in rank(diff_specs(a, b))}


def tool(ref, scope=None, reversible=None):
    return ToolUse(
        ref=ref,
        scope=Value[Scope].declared(scope) if scope else Value[Scope].unknown(),
        reversible=Value[bool].declared(reversible) if reversible is not None else Value[bool].unknown(),
    )


def test_new_tools_rank_by_scope_and_unknown_counts_as_write():
    found = severities(
        AgentSpec(),
        AgentSpec(
            tools=[
                tool("tool/write", Scope.WRITE),
                tool("tool/mystery"),
                tool("tool/run", Scope.EXECUTE),
                tool("tool/read", Scope.READ, True),
                tool("tool/irreversible-read", Scope.READ, False),
            ]
        ),
    )
    assert found["tool tool/write"] is Severity.HIGH
    assert found["tool tool/mystery"] is Severity.HIGH
    assert found["tool tool/run"] is Severity.MEDIUM
    assert found["tool tool/read"] is Severity.LOW
    assert found["tool tool/irreversible-read"] is Severity.HIGH


def test_scope_widening_is_high_and_narrowing_is_low():
    a = AgentSpec(tools=[tool("tool/x", Scope.READ), tool("tool/y", Scope.WRITE)])
    b = AgentSpec(tools=[tool("tool/x", Scope.WRITE), tool("tool/y", Scope.READ)])
    found = severities(a, b)
    assert found["tool tool/x scope"] is Severity.HIGH
    assert found["tool tool/y scope"] is Severity.LOW


def test_permissions_model_and_knowledge():
    a = AgentSpec(
        model={"requested": Value[str].declared("gpt-5")},
        knowledge=[KnowledgeUse(ref="knowledge/faq")],
    )
    b = AgentSpec(
        model={"requested": Value[str].declared("claude-sonnet")},
        permissions={
            "identity": Value[IdentityKind].declared(IdentityKind.ON_BEHALF_OF_USER),
            "grants": [
                Grant(target="store/db", privilege="WRITE", scope=Scope.WRITE),
                Grant(target="model/llm", privilege="CAN_QUERY", scope=Scope.EXECUTE),
            ],
        },
        description=Value[str].declared("new"),
    )
    found = severities(a, b)
    assert found["permissions.identity"] is Severity.HIGH
    assert found["grant store/db WRITE"] is Severity.HIGH
    assert found["grant model/llm CAN_QUERY"] is Severity.MEDIUM
    assert found["model.requested"] is Severity.HIGH
    assert found["knowledge knowledge/faq"] is Severity.MEDIUM
    assert found["description"] is Severity.LOW


def test_rank_orders_highest_first_and_keeps_ties_stable():
    a = AgentSpec()
    b = AgentSpec(
        description=Value[str].declared("d"),
        tools=[tool("tool/a", Scope.WRITE), tool("tool/b", Scope.WRITE)],
    )
    ranked = rank(diff_specs(a, b))
    assert [r.severity for r in ranked] == sorted((r.severity for r in ranked), reverse=True)
    assert [r.change.path for r in ranked][:2] == ["tool tool/a", "tool tool/b"]
    assert ranked[0].line() == "HIGH  + tool tool/a (scope write, reversible unknown)"
