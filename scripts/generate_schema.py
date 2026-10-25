"""Write the JSON Schema for ledger records to schema/marshal-v0.schema.json."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from marshal_ledger.model import AgentVersion

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schema" / "marshal-v0.schema.json"


def render() -> str:
    schema = AgentVersion.model_json_schema(by_alias=True)
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://github.com/marshal-ledger/marshal/schema/marshal-v0.schema.json",
        **schema,
    }
    return json.dumps(schema, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    SCHEMA_PATH.write_text(render(), encoding="utf-8")
    print(f"wrote {SCHEMA_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
