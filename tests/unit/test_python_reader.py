import textwrap

from marshal_ledger.model import Provenance, Scope
from marshal_ledger.readers import ReadOptions, detect
from marshal_ledger.readers.python import PythonAgentReader

FAKE_TOKEN = "dapi" + "0123456789abcdef" * 2


def project(tmp_path, files):
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
    return tmp_path


def read(tmp_path, files, **options):
    return PythonAgentReader().read(project(tmp_path, files), ReadOptions(**options))


def test_detection_requires_a_framework_import(tmp_path):
    project(tmp_path, {"util.py": "import json\n"})
    assert not PythonAgentReader.detect(tmp_path)
    (tmp_path / "agent.py").write_text("from agents import Agent\n")
    assert detect(tmp_path) is PythonAgentReader


def test_openai_agent_with_cross_module_prompt_and_tools(tmp_path):
    result = read(tmp_path, {
        "app/__init__.py": "",
        "app/prompts.py": 'BASE = "Answer billing questions."\nPROMPT = BASE + " Never issue refunds."\n',
        "app/agent.py": '''
            from agents import Agent, ModelSettings, WebSearchTool, function_tool
            from .prompts import PROMPT

            @function_tool(name_override="lookup_invoice")
            def lookup(invoice_id: str, verbose: bool = False) -> str:
                """Look up an invoice.

                More detail that is not part of the description.
                """

            billing = Agent(
                name="Billing",
                instructions=PROMPT,
                model="gpt-5",
                model_settings=ModelSettings(temperature=0.2),
                tools=[lookup, WebSearchTool()],
            )
            triage = Agent(name="Triage", instructions="Route the user.", handoffs=[billing])
        ''',
    })
    agents = {r.spec.display_name.value: r for r in result.records}
    billing = agents["Billing"].spec
    assert billing.instructions.text == "Answer billing questions. Never issue refunds."
    assert billing.instructions.provenance is Provenance.DECLARED
    assert (billing.model.requested.value, billing.model.provider.value) == ("gpt-5", "openai")
    assert billing.model.provider.provenance is Provenance.DERIVED
    assert billing.model.parameters == {"temperature": 0.2}
    tools = {t.ref: t for t in billing.tools}
    lookup = tools["tool/python.app.agent:lookup"]
    assert (lookup.name, lookup.description) == ("lookup_invoice", "Look up an invoice.")
    assert lookup.input_schema == {
        "type": "object",
        "properties": {"invoice_id": {"type": "string"}, "verbose": {"type": "boolean"}},
        "required": ["invoice_id"],
    }
    assert tools["tool/openai.web_search"].scope.value is Scope.READ
    assert [t.ref for t in agents["Triage"].spec.tools] == [f"agent/{agents['Billing'].agent}"]


def test_runtime_values_are_unknown_with_reasons(tmp_path):
    result = read(tmp_path, {
        "agent.py": '''
            import os
            from google.adk.agents import Agent

            def build(tools):
                return Agent(name="dynamic", model=os.getenv("MODEL"), instruction=load_prompt(), tools=tools)
        ''',
    })
    [record] = result.records
    spec = record.spec
    assert spec.model.requested.provenance is Provenance.UNKNOWN
    assert "environment variable MODEL" in spec.model.requested.basis
    assert spec.instructions.provenance is Provenance.UNKNOWN
    assert record.spec.extensions["python"]["tools_unresolved"] == ["parameter tools (agent.py:6)"]


def test_langchain_models_and_databricks_tools(tmp_path):
    result = read(tmp_path, {
        "graph.py": f'''
            from databricks_langchain import ChatDatabricks, UCFunctionToolkit, VectorSearchRetrieverTool
            from langchain.agents import create_agent

            toolkit = UCFunctionToolkit(function_names=["main.billing.refund", "main.billing.lookup"])
            retriever = VectorSearchRetrieverTool(index_name="main.billing.docs_index")
            agent = create_agent(
                model=ChatDatabricks(endpoint="databricks-claude-sonnet-5-5", temperature=0),
                tools=toolkit.tools + [retriever],
                system_prompt="Use token {FAKE_TOKEN} for nothing.",
            )
        ''',
    })
    [record] = result.records
    spec = record.spec
    assert (spec.model.requested.value, spec.model.provider.value) == ("databricks-claude-sonnet-5-5", "databricks")
    assert spec.model.parameters == {"temperature": 0}
    refs = {t.ref for t in spec.tools}
    assert {"tool/databricks.uc.function.main.billing.refund", "tool/databricks.uc.function.main.billing.lookup"} <= refs
    assert [k.ref for k in spec.knowledge] == ["knowledge/databricks.uc.vector_index.main.billing.docs_index"]
    assert FAKE_TOKEN not in spec.instructions.text
    assert record.agent.endswith("/agent")


def test_code_is_never_executed(tmp_path):
    marker = tmp_path / "executed"
    read(tmp_path, {
        "agent.py": f'''
            open({str(marker)!r}, "w").write("ran")
            from agents import Agent
            Agent(name="safe", instructions="x")
        ''',
    })
    assert not marker.exists()


def test_custom_graphs_are_reported(tmp_path):
    result = read(tmp_path, {
        "graph.py": '''
            from langgraph.graph import StateGraph
            builder = StateGraph(dict)
        ''',
    })
    assert result.records == []
    assert "StateGraph" in result.skipped[0].reason


def test_databricks_app_links_to_its_code(tmp_path):
    from marshal_ledger.readers.databricks import DatabricksBundleReader

    root = project(tmp_path, {
        "databricks.yml": """
            bundle: {name: billing}
            resources:
              apps:
                billing_agent:
                  name: billing-agent
                  source_code_path: ./app
                  resources:
                    - name: llm
                      serving_endpoint: {name: databricks-claude-sonnet-5-5, permission: CAN_QUERY}
        """,
        "app/agent.py": '''
            from agents import Agent
            Agent(name="Billing", instructions="Answer billing questions.", model="databricks-claude-sonnet-5-5")
        ''',
    })
    [record] = DatabricksBundleReader().read(root).records
    assert record.spec.instructions.text == "Answer billing questions."
    assert record.spec.instructions.basis == "app/agent.py:3"
    assert record.spec.extensions["databricks"]["code"]["framework"] == "openai-agents"
    unlinked = DatabricksBundleReader().read(root, ReadOptions(link_code=False)).records[0]
    assert unlinked.spec.instructions.provenance is Provenance.UNKNOWN
