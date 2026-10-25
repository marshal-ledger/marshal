from marshal_ledger.model import AgentSpec, IdentityKind, Scope, ToolUse, Value
from marshal_ledger.quality import score


def test_empty_spec_is_fully_unknown():
    quality = score(AgentSpec())
    assert quality.completeness == 0.0
    assert "permissions.identity" in quality.unknown_fields


def test_unknown_write_tool_weighs_more_than_description():
    base = AgentSpec(permissions={"identity": Value[IdentityKind].declared(IdentityKind.SERVICE_PRINCIPAL)})
    with_description = base.model_copy(deep=True)
    with_description.description = Value[str].declared("Reconciles invoices")
    with_tool_scope = base.model_copy(deep=True)
    with_tool_scope.tools = [ToolUse(ref="tool/x", scope=Value[Scope].declared(Scope.WRITE))]
    without_tool_scope = base.model_copy(deep=True)
    without_tool_scope.tools = [ToolUse(ref="tool/x")]

    assert score(with_description).completeness > score(base).completeness
    assert score(with_tool_scope).completeness > score(without_tool_scope).completeness
    assert "tools[0].scope" in score(without_tool_scope).unknown_fields
