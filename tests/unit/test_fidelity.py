from marshal_ledger.fidelity import Outcome, compare, field_values, matches
from marshal_ledger.model import AgentSpec, Scope, Source, ToolUse, Value
from marshal_ledger.readers.base import build_record


def record(**spec):
    return build_record(agent="a", platform="p", source=Source(format="f", artifact="x"), spec=AgentSpec(**spec))


def test_outcomes():
    source = record(
        description=Value[str].declared("d"),
        tools=[ToolUse(ref="tool/x", scope=Value[Scope].declared(Scope.WRITE)), ToolUse(ref="tool/y")],
    )
    written = record(
        description=Value[str].declared("changed"),
        display_name=Value[str].declared("filled in"),
        tools=[ToolUse(ref="tool/x")],
    )
    report = compare(source, written, "fmt", {"tools[*]": "no tools", "display_name": "required"})
    outcomes = {f.path: (f.outcome, f.note) for f in report.fields}
    assert outcomes["description"][0] is Outcome.APPROXIMATED
    assert outcomes["tools[tool/x].scope"][0] is Outcome.DROPPED
    assert outcomes["tools[tool/y]"] == (Outcome.DROPPED, "no tools")
    assert outcomes["display_name"] == (Outcome.DEFAULTED, "required")
    assert outcomes["agent"][0] is Outcome.LOSSLESS
    assert report.summary() == "3 lossless · 1 approximated · 2 dropped · 1 defaulted"


def test_field_values_skip_unknowns():
    values = field_values(record(tools=[ToolUse(ref="tool/x")]))
    assert values == {"agent": "a", "platform": "p", "tools[tool/x]": True}


def test_patterns_treat_brackets_literally():
    assert matches("permissions.grants[store/db WRITE]", "permissions.grants[*]")
    assert matches("extensions.databricks.command", "extensions.databricks.*")
    assert not matches("permissions.grantsX", "permissions.grants[*]")


def test_ignored_mechanics_are_not_reported():
    source = record()
    written = record(extensions={"fmt": {"format_version": "1"}})
    report = compare(source, written, "fmt", {}, ignore=("extensions.fmt.format_version",))
    assert report.lossy == []


def test_defaulted_fields_keep_read_back_order():
    # A set difference would order them by string hash, which changes from run to run.
    written = record(
        display_name=Value[str].declared("filled in"),
        tools=[ToolUse(ref=f"tool/{name}", requires_confirmation=Value[bool].declared(False)) for name in "zyxwvu"],
    )
    report = compare(record(), written, "fmt", {})
    defaulted = [f.path for f in report.fields if f.outcome is Outcome.DEFAULTED]
    assert defaulted == [p for p in field_values(written) if p in defaulted]
    assert len(defaulted) > 6
