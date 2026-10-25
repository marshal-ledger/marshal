import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_committed_schema_matches_model():
    spec = importlib.util.spec_from_file_location("generate_schema", ROOT / "scripts" / "generate_schema.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    committed = (ROOT / "schema" / "marshal-v0.schema.json").read_text(encoding="utf-8")
    assert committed == module.render(), "schema is stale; run python scripts/generate_schema.py"
