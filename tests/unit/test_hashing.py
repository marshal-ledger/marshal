from marshal_ledger.hashing import sha256_text, spec_digest
from marshal_ledger.model import AgentSpec, Instructions, Provenance, Scope, ToolUse, Value


def spec(**overrides):
    base = dict(
        display_name=Value[str].declared("Invoice agent", "agent.yml:name"),
        instructions=Instructions(
            hash=sha256_text("Reconcile invoices."), text="Reconcile invoices.", provenance=Provenance.DECLARED
        ),
        tools=[
            ToolUse(ref="tool/sap.update_po", scope=Value[Scope].declared(Scope.WRITE)),
            ToolUse(ref="tool/sap.read_po", scope=Value[Scope].declared(Scope.READ)),
        ],
    )
    base.update(overrides)
    return AgentSpec(**base)


def test_same_definition_same_version():
    assert spec_digest(spec()) == spec_digest(spec())


def test_tool_order_does_not_change_version():
    reordered = spec()
    reordered.tools.reverse()
    assert spec_digest(reordered) == spec_digest(spec())


def test_basis_and_provenance_do_not_change_version():
    moved = spec(display_name=Value[str].derived("Invoice agent", "some other rule"))
    assert spec_digest(moved) == spec_digest(spec())


def test_hash_only_mode_gives_same_version():
    hashed = spec()
    hashed.instructions.text = None
    assert spec_digest(hashed) == spec_digest(spec())


def test_instruction_change_changes_version():
    changed = spec(
        instructions=Instructions(
            hash=sha256_text("Reconcile invoices daily."), text="Reconcile invoices daily.", provenance=Provenance.DECLARED
        )
    )
    assert spec_digest(changed) != spec_digest(spec())


def test_extension_list_order_is_significant():
    a = spec(extensions={"databricks": {"command": ["uv", "run", "app"]}})
    b = spec(extensions={"databricks": {"command": ["run", "uv", "app"]}})
    assert spec_digest(a) != spec_digest(b)


def test_extension_keys_named_like_metadata_are_kept():
    a = spec(extensions={"x": {"basis": 1}})
    b = spec(extensions={"x": {"basis": 2}})
    assert spec_digest(a) != spec_digest(b)
