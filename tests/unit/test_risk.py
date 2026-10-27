from marshal_ledger.diff import diff_specs
from marshal_ledger.model import AgentSpec, Grant, IdentityKind, Instructions, KnowledgeUse, Scope, ToolUse, Value
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
    assert found["model.requested"] is Severity.MEDIUM
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


def test_security_relevant_extension_changes_rank_high():
    a = AgentSpec(extensions={"copilot-studio": {"actions": {"Search": {"connection_mode": "Invoker"}}, "topics": {"x": 1}}})
    b = AgentSpec(extensions={"copilot-studio": {"actions": {"Search": {"connection_mode": "Maker"}}, "topics": {"x": 2}}})
    found = severities(a, b)
    assert found["extensions.copilot-studio.actions.Search.connection_mode"] is Severity.HIGH
    assert found["extensions.copilot-studio.topics.x"] is Severity.LOW


def test_a_new_model_provider_is_high_and_an_upgrade_within_one_is_medium():
    def spec(model, provider):
        return AgentSpec(model={"requested": Value[str].declared(model), "provider": Value[str].declared(provider)})

    upgrade = severities(spec("claude-sonnet-5", "anthropic"), spec("claude-sonnet-5-5", "anthropic"))
    assert upgrade == {"model.requested": Severity.MEDIUM}
    switch = severities(spec("claude-sonnet-5-5", "anthropic"), spec("gpt-5", "openai"))
    assert switch == {"model.requested": Severity.MEDIUM, "model.provider": Severity.HIGH}


def instructions(text):
    return AgentSpec(instructions=Instructions(hash="sha256:" + str(hash(text)), text=text))


GUARDED = "Match invoices to purchase orders.\nNever approve payment without explicit AP approval.\nBe concise."


def test_changed_instructions_are_medium():
    found = severities(instructions(GUARDED), instructions(GUARDED.replace("Be concise.", "Be brief.")))
    assert found == {"instructions": Severity.MEDIUM}


def test_instructions_that_lose_a_guardrail_are_high():
    [ranked] = rank(diff_specs(instructions(GUARDED), instructions("Match invoices to purchase orders.\nBe concise.")))
    assert ranked.severity is Severity.HIGH
    assert ranked.change.guardrails == "never, approve, approval"
    assert ranked.line() == "HIGH  ~ instructions: +0 -1 lines; removed a line with 'never', 'approve', 'approval'"


def test_a_guardrail_reworded_in_place_is_not_a_removal():
    reworded = GUARDED.replace("Never approve payment without explicit AP approval.",
                               "Never approve a payment without approval from AP.")
    assert severities(instructions(GUARDED), instructions(reworded)) == {"instructions": Severity.MEDIUM}


def test_hash_only_instructions_are_medium():
    # Without the text, a removed guardrail can't be seen, so the change is reviewed but not escalated.
    a = AgentSpec(instructions=Instructions(hash="sha256:aa"))
    b = AgentSpec(instructions=Instructions(hash="sha256:bb"))
    assert severities(a, b) == {"instructions": Severity.MEDIUM}


def test_ranked_change_with_guardrails_survives_a_round_trip():
    [ranked] = rank(diff_specs(instructions(GUARDED), instructions("Be concise.")))
    assert type(ranked).from_dict(ranked.to_dict()) == ranked
