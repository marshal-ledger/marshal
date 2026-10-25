"""Content addressing for agent versions.

A version hash covers the values in an :class:`~marshal_ledger.model.AgentSpec`
and nothing else. Excluded by design:

- ``bindings``, which hold environment-bound values, so one definition deployed
  to dev and prod is one version.
- ``basis`` strings, which describe where a value was found, so moving a file
  does not create a new version.
- ``provenance`` labels, so a reader that learns to classify a value it already
  read does not create a new version.
- Instruction text, which is represented by its hash. Hash-only mode therefore
  produces the same version as full mode.

Absent values (``None``, empty lists and empty mappings) are dropped, so adding
an optional field to the model does not change existing versions.

Tools, knowledge sources, grants and access rules are sorted, so reordering
them in the source does not create a new version. Other lists, such as those
inside platform extensions, keep their order.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .model import AgentSpec

_METADATA_KEYS = {"provenance", "basis"}


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _absent(value: Any) -> bool:
    return value is None or value == {} or value == []


def _strip(node: Any) -> Any:
    if isinstance(node, dict):
        stripped = {k: _strip(v) for k, v in node.items() if k not in _METADATA_KEYS}
        return {k: v for k, v in stripped.items() if not _absent(v)}
    if isinstance(node, list):
        return [_strip(v) for v in node]
    return node


def _sorted(items: list[Any]) -> list[Any]:
    return sorted(items, key=canonical_json)


def spec_digest(spec: AgentSpec) -> str:
    data = spec.model_dump(mode="json")
    extensions = data.pop("extensions")  # platform fields are hashed verbatim
    data = _strip(data)
    data.get("instructions", {}).pop("text", None)
    for key in ("tools", "knowledge"):
        if key in data:
            data[key] = _sorted(data[key])
    permissions = data.get("permissions", {})
    for key in ("grants", "access"):
        if key in permissions:
            permissions[key] = _sorted(permissions[key])
    data = {k: v for k, v in data.items() if not _absent(v)}
    if extensions:
        data["extensions"] = extensions
    return sha256_text(canonical_json(data))
