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


def _strip(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip(v) for k, v in node.items() if k not in _METADATA_KEYS}
    if isinstance(node, list):
        return [_strip(v) for v in node]
    return node


def _sorted(items: list[Any]) -> list[Any]:
    return sorted(items, key=canonical_json)


def spec_digest(spec: AgentSpec) -> str:
    data = spec.model_dump(mode="json")
    extensions = data.pop("extensions")  # platform fields are hashed verbatim
    data = _strip(data)
    data["extensions"] = extensions
    data["instructions"].pop("text", None)
    data["tools"] = _sorted(data["tools"])
    data["knowledge"] = _sorted(data["knowledge"])
    data["permissions"]["grants"] = _sorted(data["permissions"]["grants"])
    data["permissions"]["access"] = _sorted(data["permissions"]["access"])
    return sha256_text(canonical_json(data))
