import json
import textwrap

from marshal_ledger.cli import main

BUNDLE = """
    bundle: {name: support}
    resources:
      apps:
        support_agent:
          name: support-agent
          resources:
            - name: llm
              serving_endpoint: {name: databricks-claude-sonnet-5-5, permission: CAN_QUERY}
            - name: data
              uc_securable: {securable_full_name: main.support.faq, securable_type: TABLE, permission: SELECT}
    targets:
      prod: {mode: production}
"""


def run(capsys, *argv):
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_collect_ls_diff_log(tmp_path, capsys):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "databricks.yml").write_text(textwrap.dedent(BUNDLE))
    db = str(tmp_path / "ledger.sqlite")

    code, out, _ = run(capsys, "collect", str(bundle), "--target", "prod", "--event", "shipped", "--db", db)
    assert code == 0 and out.splitlines()[0] == "shipped prod: support/support_agent v1 (new version)"

    (bundle / "databricks.yml").write_text(
        textwrap.dedent(BUNDLE).replace(
            "{securable_full_name: main.support.faq, securable_type: TABLE, permission: SELECT}",
            "{securable_full_name: main.support.refund, securable_type: FUNCTION, permission: EXECUTE}",
        )
    )
    report = tmp_path / "comment.md"
    code, out, _ = run(
        capsys, "collect", str(bundle), "--target", "prod", "--event", "proposed", "--db", db, "--report", str(report)
    )
    assert "v2 (new version)" in out
    assert "support/support_agent (databricks)  prod: v1 → v2" in out
    comment = report.read_text()
    assert comment.startswith("<!-- marshal-ledger:report -->")
    assert "MED   + tool tool/databricks.uc.function.main.support.refund" in comment

    code, out, _ = run(capsys, "ls", "--db", db)
    assert "support/support_agent  databricks  v2      prod v1" in out

    code, out, _ = run(capsys, "ls", "--tool", "main.support.*", "--output", "json", "--db", db)
    assert [a["agent"] for a in json.loads(out)] == ["support/support_agent"]

    code, out, _ = run(capsys, "diff", "support/support_agent", "--db", db)
    assert out.splitlines()[0] == "support/support_agent (databricks)  v1 → v2"
    assert "  MED   + tool tool/databricks.uc.function.main.support.refund (scope execute, reversible unknown)" in out
    assert "  MED   - knowledge knowledge/databricks.uc.table.main.support.faq (scope read)" in out

    code, out, _ = run(capsys, "diff", "support/support_agent@v1", "support/support_agent@v2", "--output", "json", "--db", db)
    assert json.loads(out)["from"] == "v1"

    code, out, _ = run(capsys, "log", "support/support_agent", "--db", db)
    assert [line.split()[2] for line in out.splitlines()] == ["shipped", "proposed"]


def test_collect_requires_environment(tmp_path, capsys):
    (tmp_path / "databricks.yml").write_text(textwrap.dedent(BUNDLE))
    code, _, err = run(capsys, "collect", str(tmp_path), "--event", "shipped", "--db", str(tmp_path / "x.sqlite"))
    assert code == 2 and "--environment" in err


def test_unknown_agent_is_an_error(tmp_path, capsys):
    code, _, err = run(capsys, "diff", "nobody", "--db", str(tmp_path / "x.sqlite"))
    assert code == 1 and "unknown agent" in err


def test_convert(tmp_path, capsys):
    import yaml

    adl = tmp_path / "agent.adl.yaml"
    adl.write_text(
        yaml.safe_dump(
            {
                "adl_spec": "0.3.0",
                "name": "Calculator",
                "description": "Adds numbers.",
                "version": "1.0.0",
                "data_classification": {"sensitivity": "public"},
                "tools": [{"name": "add", "description": "Add two numbers.", "read_only": True}],
            }
        )
    )
    code, out, err = run(capsys, "convert", str(adl), "--to", "agent-spec")
    assert code == 0
    assert yaml.safe_load(out)["component_type"] == "Agent"
    assert "calculator → agent-spec" in err and "Fidelity:" in err

    report = tmp_path / "fidelity.json"
    code, _, err = run(capsys, "convert", str(adl), "--to", "oasf", "--out", str(tmp_path / "out"), "--report", str(report))
    assert code == 0 and (tmp_path / "out" / "calculator.oasf.json").exists()
    assert json.loads(report.read_text())[0]["target"] == "oasf"
    assert "warning: OASF requires at least one skill" in err


def test_db_auth_needs_postgres_and_reports_an_unreachable_store(tmp_path, capsys):
    try:
        main(["ls", "--db", str(tmp_path / "ledger.sqlite"), "--db-auth", "azure"])
    except SystemExit as exc:
        assert exc.code == 2
    assert "only to a postgresql:// store" in capsys.readouterr().err
    code, _, err = run(capsys, "ls", "--db", "postgresql://marshal@127.0.0.1:1/marshal?connect_timeout=2")
    assert code == 1 and "cannot connect to the Postgres store" in err
