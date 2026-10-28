"""The Copilot Studio writer, on an agent from another format that uses a standard connector."""

import textwrap

import yaml

from marshal_ledger.convert import convert
from marshal_ledger.readers import READERS

AGENT = textwrap.dedent("""
    marshal: agent/v0
    id: import-test/weather-helper
    platform: in-house
    display_name: Weather Helper
    instructions: |
      You answer questions about today's weather.
      Never guess a forecast without calling the tool.
    tools:
      - ref: tool/powerplatform.shared_msnweather.TodaysForecast
        name: Get today's forecast
        scope: read
    permissions:
      identity: on_behalf_of_user
""")


def conversion(tmp_path):
    path = tmp_path / "weather-helper.marshal.yaml"
    path.write_text(AGENT)
    [record] = READERS["marshal-file"]().read(path).records
    return convert(record, "copilot-studio")


def test_connection_mode_defaults_from_identity_and_says_so(tmp_path):
    result = conversion(tmp_path)
    action = yaml.safe_load(result.files["actions/GetTodaySForecast.mcs.yml"])["action"]
    assert action["connectionProperties"] == {"mode": "Invoker"}
    notes = {f.path: f.note for f in result.report.lossy}
    assert "agent's identity" in notes["extensions.copilot-studio.actions"]


def test_multi_line_instructions_are_written_as_a_block(tmp_path):
    text = conversion(tmp_path).files["agent.mcs.yml"]
    assert "instructions: |" in text
    assert "  Never guess a forecast without calling the tool.\n" in text


def test_a_url_is_a_sharepoint_source_only_when_its_host_is_on_sharepoint(tmp_path):
    path = tmp_path / "sites.marshal.yaml"
    path.write_text(AGENT + textwrap.dedent("""
        knowledge:
          - ref: knowledge/https://contoso.sharepoint.com/sites/hr
            name: HR site
          - ref: knowledge/https://example.com/docs?from=contoso.sharepoint.com
            name: Public docs
          - ref: knowledge/https://contoso.sharepoint.com.example.net/
            name: Lookalike
    """))
    [record] = READERS["marshal-file"]().read(path).records
    files = convert(record, "copilot-studio").files
    kinds = {name: yaml.safe_load(text)["kind"] for name, text in files.items() if name.startswith("knowledge/")}
    assert kinds == {
        "knowledge/HrSite.mcs.yml": "SharePointSearchSource",
        "knowledge/PublicDocs.mcs.yml": "PublicSiteSearchSource",
        "knowledge/Lookalike.mcs.yml": "PublicSiteSearchSource",
    }
