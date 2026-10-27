"""Written ADK Agent Configs load in ADK itself. Runs only where google-adk is installed:

    pip install google-adk mcp && pytest tests/golden/test_adk_load.py

ADK imports the Python functions a config names, and those live in the agent's own
project, so a missing module is expected. So is ADK's refusal of stdio MCP servers in
configs (from google-adk 2.13), which applies to its own stdio sample too.
"""

import warnings

import pytest

from test_translation import SOURCES, records

from marshal_ledger.convert import convert

pytest.importorskip("google.adk")
pytest.importorskip("mcp")


def expected(error: Exception) -> bool:
    return isinstance(error, ModuleNotFoundError) or "Stdio MCP servers are not allowed" in str(error)


@pytest.mark.parametrize("fmt, fixture", SOURCES)
def test_written_configs_load_in_adk(fmt, fixture, tmp_path):
    from google.adk.agents.config_agent_utils import from_config

    for i, record in enumerate(records(fmt, fixture)):
        folder = tmp_path / str(i)
        folder.mkdir()
        conversion = convert(record, "adk-config")
        for name, text in conversion.files.items():
            (folder / name).write_text(text)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                from_config(str(folder / "root_agent.yaml"))
            except Exception as error:  # anything else is a writer bug
                assert expected(error), f"{record.agent}: {type(error).__name__}: {error}"
