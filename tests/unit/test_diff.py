from marshal_ledger.diff import diff_specs, format_changes
from marshal_ledger.hashing import sha256_text
from marshal_ledger.model import (
    AgentSpec,
    Grant,
    IdentityKind,
    Instructions,
    KnowledgeUse,
    Provenance,
    Scope,
    ToolUse,
    Value,
)


def instructions(text, hash_only=False):
    return Instructions(hash=sha256_text(text), text=None if hash_only else text, provenance=Provenance.DECLARED)


def test_identical_specs_have_no_changes():
    spec = AgentSpec(description=Value[str].declared("x"))
    assert diff_specs(spec, spec.model_copy(deep=True)) == []


def test_provenance_and_basis_alone_are_not_changes():
    a = AgentSpec(description=Value[str].declared("x", "a.yml"))
    b = AgentSpec(description=Value[str].derived("x", "rule"))
    assert diff_specs(a, b) == []


def test_tools_knowledge_and_grants_are_matched_by_ref():
    a = AgentSpec(
        tools=[ToolUse(ref="tool/read", scope=Value[Scope].declared(Scope.READ))],
        knowledge=[KnowledgeUse(ref="knowledge/faq")],
        permissions={"grants": [Grant(target="tool/read", privilege="EXECUTE", scope=Scope.EXECUTE)]},
    )
    b = AgentSpec(
        tools=[
            ToolUse(ref="tool/read", scope=Value[Scope].declared(Scope.WRITE)),
            ToolUse(ref="tool/update_po", scope=Value[Scope].declared(Scope.WRITE)),
        ],
        permissions={
            "identity": Value[IdentityKind].declared(IdentityKind.SERVICE_PRINCIPAL),
            "grants": [Grant(target="tool/read", privilege="EXECUTE", scope=Scope.EXECUTE)],
        },
    )
    changes = {(c.kind, c.path): c for c in diff_specs(a, b)}
    assert ("added", "tool tool/update_po") in changes
    assert ("removed", "knowledge knowledge/faq") in changes
    scope = changes[("changed", "tool tool/read scope")]
    assert (scope.before, scope.after) == ("read", "write")
    identity = changes[("changed", "permissions.identity")]
    assert (identity.before, identity.after) == ("unknown", "service_principal")
    assert not any("grant" in path for _, path in changes)


def test_changes_are_grouped_permissions_first():
    a = AgentSpec(description=Value[str].declared("old"))
    b = AgentSpec(
        description=Value[str].declared("new"),
        permissions={"grants": [Grant(target="store/db", privilege="WRITE", scope=Scope.WRITE)]},
    )
    assert [c.category for c in diff_specs(a, b)] == ["permissions", "metadata"]


def test_instruction_changes_count_lines_or_report_hash_only():
    a = AgentSpec(instructions=instructions("Be brief.\nCite sources."))
    b = AgentSpec(instructions=instructions("Be brief.\nCite sources.\nNever issue refunds."))
    [change] = diff_specs(a, b)
    assert change.detail == "+1 -0 lines"

    hashed = AgentSpec(instructions=instructions("Be brief.", hash_only=True))
    [change] = diff_specs(a, hashed)
    assert change.detail == "content changed"


def test_extension_changes_use_dotted_paths():
    a = AgentSpec(extensions={"databricks": {"command": ["run"], "compute_size": "MEDIUM"}})
    b = AgentSpec(extensions={"databricks": {"command": ["run"], "compute_size": "LARGE", "env": [{"name": "X"}]}})
    paths = {(c.kind, c.path) for c in diff_specs(a, b)}
    assert paths == {("changed", "extensions.databricks.compute_size"), ("added", "extensions.databricks.env")}


def test_format():
    a = AgentSpec()
    b = AgentSpec(tools=[ToolUse(ref="tool/x")])
    text = format_changes("agent  v1 → v2", diff_specs(a, b))
    assert text.splitlines() == ["agent  v1 → v2", "  + tool tool/x (scope unknown, reversible unknown)"]
    assert format_changes("h", []).endswith("no changes")
