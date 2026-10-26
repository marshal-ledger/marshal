"""Copilot Studio solutions (``pac solution export``), unpacked or as a zip."""

import zipfile
from pathlib import Path

from marshal_ledger.model import IdentityKind
from marshal_ledger.readers import detect
from marshal_ledger.readers.copilotstudio import CopilotStudioReader, _extract

FILES = {
    "bots/demo_agent/bot.xml": """<bot schemaname="demo_agent">
  <accesscontrolpolicy>1</accesscontrolpolicy>
  <authenticationmode>1</authenticationmode>
  <authenticationtrigger>0</authenticationtrigger>
  <language>1033</language>
  <name>Demo Agent</name>
</bot>""",
    "bots/demo_agent/configuration.json": '{"$kind": "BotConfiguration", "gPTSettings": {"defaultSchemaName": "demo_agent.gpt.default"}}',
    "botcomponents/demo_agent.gpt.default/botcomponent.xml": """<botcomponent schemaname="demo_agent.gpt.default">
  <componenttype>15</componenttype>
  <description>Answers questions about orders.</description>
  <name>Demo Agent</name>
  <parentbotid><schemaname>demo_agent</schemaname></parentbotid>
</botcomponent>""",
    "botcomponents/demo_agent.gpt.default/data": "kind: GptComponentMetadata\ninstructions: Help with orders.\n",
    "botcomponents/demo_agent.action.SendMail/botcomponent.xml": """<botcomponent schemaname="demo_agent.action.SendMail">
  <componenttype>9</componenttype>
  <name>Send an email</name>
  <parentbotid><schemaname>demo_agent</schemaname></parentbotid>
</botcomponent>""",
    "botcomponents/demo_agent.action.SendMail/data": """kind: TaskDialog
modelDisplayName: Send an email
action:
  kind: InvokeConnectorTaskAction
  connectionReference: demo_agent.shared_office365.abc
  operationId: SendEmailV2
""",
    "botcomponents/demo_agent.topic.LookUp/botcomponent.xml": """<botcomponent schemaname="demo_agent.topic.LookUp">
  <componenttype>9</componenttype>
  <name>Look up an order</name>
  <parentbotid><schemaname>demo_agent</schemaname></parentbotid>
</botcomponent>""",
    "botcomponents/demo_agent.topic.LookUp/data": """kind: AdaptiveDialog
beginDialog:
  kind: OnRecognizedIntent
  id: main
  actions:
    - kind: InvokeConnectorAction
      id: lookup
      input:
        binding:
          entityName: orders
      output:
        kind: SingleVariableOutputBinding
        schema:
          @odata.type: String
      connectionReference: demo_agent.dataverse
      connectionProperties:
        mode: Invoker
      operationId: ListRecords
""",
    "botcomponents/demo_agent.file.Policies/botcomponent.xml": """<botcomponent schemaname="demo_agent.file.Policies">
  <componenttype>14</componenttype>
  <name>Policies.pdf</name>
  <parentbotid><schemaname>demo_agent</schemaname></parentbotid>
</botcomponent>""",
    "Other/Customizations.xml": """<ImportExportXml>
  <connectionreferences>
    <connectionreference connectionreferencelogicalname="demo_agent.dataverse">
      <connectorid>/providers/Microsoft.PowerApps/apis/shared_commondataserviceforapps</connectorid>
    </connectionreference>
  </connectionreferences>
</ImportExportXml>""",
}


def write_solution(root: Path) -> Path:
    for name, content in FILES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return root


def zip_solution(root: Path, target: Path, extra: dict[str, str] | None = None) -> Path:
    with zipfile.ZipFile(target, "w") as archive:
        for name, content in FILES.items():
            archive.writestr("customizations.xml" if name == "Other/Customizations.xml" else name, content)
        for name, content in (extra or {}).items():
            archive.writestr(name, content)
    return target


def test_unpacked_solution(tmp_path):
    solution = write_solution(tmp_path / "src")
    assert detect(tmp_path) is CopilotStudioReader
    result = CopilotStudioReader().read(tmp_path)
    assert result.warnings == []
    [record] = result.records
    spec = record.spec
    assert record.agent == "demo_agent"
    assert record.source.artifact == "src/bots/demo_agent/bot.xml"
    assert spec.display_name.value == "Demo Agent"
    assert spec.description.value == "Answers questions about orders."
    assert spec.instructions.text == "Help with orders."
    # bot.xml stores choices as integers; they map to the names a workspace uses.
    settings = spec.extensions["copilot-studio"]["settings"]
    assert settings["accessControlPolicy"] == "ChatbotReaders"
    assert settings["authenticationMode"] == "None"
    assert settings["authenticationTrigger"] == "AsNeeded"
    assert {a.principal for a in spec.permissions.access} == {"users the agent is shared with", "unauthenticated users"}
    refs = {t.ref for t in spec.tools}
    assert refs == {
        "tool/powerplatform.shared_office365.SendEmailV2",  # an action, with the default (maker) connection
        "tool/powerplatform.shared_commondataserviceforapps.ListRecords",  # a topic call, despite the `@odata.type` key
    }
    calls = spec.extensions["copilot-studio"]["topic_calls"]
    assert calls["tool/powerplatform.shared_commondataserviceforapps.ListRecords"]["credentials"] == "end user"
    assert spec.permissions.identity.value is IdentityKind.MIXED
    assert [k.ref for k in spec.knowledge] == ["knowledge/copilot-studio.demo_agent.file.Policies.pdf"]
    assert record.bindings["connection_references"] == ["demo_agent.dataverse"]
    assert solution.is_dir()


def test_zip_reads_the_same_as_unpacked(tmp_path):
    unpacked = CopilotStudioReader().read(write_solution(tmp_path / "src")).records
    archive = zip_solution(tmp_path, tmp_path / "Demo_1_0_0_1.zip")
    assert detect(archive) is CopilotStudioReader
    [record] = CopilotStudioReader().read(archive).records
    assert record.version == unpacked[0].version
    assert record.source.artifact == "Demo_1_0_0_1.zip/bots/demo_agent/bot.xml"


def test_zip_ignores_unsafe_and_unrelated_members(tmp_path):
    archive = zip_solution(tmp_path, tmp_path / "solution.zip", {
        "../outside.txt": "nope",
        "bots/../../escape.txt": "nope",
        "/absolute.txt": "nope",
        "Workflows/flow.json": "{}",
    })
    out = tmp_path / "work" / "out"
    out.mkdir(parents=True)
    _extract(archive, out)
    extracted = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}
    assert extracted == {("customizations.xml" if n == "Other/Customizations.xml" else n) for n in FILES}
    assert not any(p.is_file() for p in (tmp_path / "work").iterdir())
    [record] = CopilotStudioReader().read(archive).records
    assert record.agent == "demo_agent"


def test_a_zip_without_an_agent_is_not_detected(tmp_path):
    archive = tmp_path / "other.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("solution.xml", "<ImportExportXml/>")
    assert not CopilotStudioReader.detect(archive)


def test_flows_leave_identity_unknown(tmp_path):
    root = tmp_path / "s"
    files = dict(FILES)
    files["botcomponents/demo_agent.action.SendMail/data"] = """kind: TaskDialog
action:
  kind: InvokeFlowTaskAction
  flowId: 11111111-2222-3333-4444-555555555555
"""
    files["botcomponents/demo_agent.topic.LookUp/data"] = "kind: AdaptiveDialog\nbeginDialog:\n  kind: OnRecognizedIntent\n  id: main\n"
    for name, content in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(content)
    [record] = CopilotStudioReader().read(root).records
    identity = record.spec.permissions.identity
    assert identity.value is None and "flows" in identity.basis
