"""Writer for Marshal files: hand-written agent definitions.

Every ledger field has a place in a Marshal file, so the only losses are
values the reader would not copy back, such as credential-like values in
extensions. Converting a record to a Marshal file gives a person a starting
point to check against the platform; once read back, its values are attested.
"""

from __future__ import annotations

from typing import Any

from ..model import AgentVersion, Value
from ..readers.marshalfile import FORMAT, KIND, UNKNOWN, clean, scrub_block
from .base import Writer, Written, scope_value, yaml_text

HEADER = (
    "# Marshal file: a hand-written agent definition (docs/formats/marshal-file.md).\n"
    "# Values here are recorded as attested by a person. Check each one against the\n"
    "# platform, set attested_by and attested_at, and write `unknown` for anything you can't confirm.\n"
)


def _known(value: Value) -> Any:
    return scope_value(value.value) if value.is_known else None


def _stated(value: Value) -> Any:
    """A value for a field the ledger scores: written as ``unknown`` when missing, so the gap shows."""
    return scope_value(value.value) if value.is_known else UNKNOWN


def _compact(block: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in block.items() if v is not None and v != {} and v != []}


class MarshalFileWriter(Writer):
    format = FORMAT
    suffix = ".marshal.yaml"
    reader_format = FORMAT

    def write(self, record: AgentVersion) -> Written:
        spec = record.spec
        notes: dict[str, str] = {}
        withheld = "credential-like values are not copied into a Marshal file"

        def text(value: Any, path: str) -> Any:
            if isinstance(value, str) and clean(value) != value:
                notes[path] = withheld
            return value

        def block(value: Any, path: str) -> Any:
            if value and scrub_block(value) != value:
                notes[path] = withheld
            return value

        doc: dict[str, Any] = {"marshal": KIND, "id": record.agent, "platform": record.platform}
        doc["display_name"] = text(_known(spec.display_name), "display_name")
        doc["description"] = text(_known(spec.description), "description")
        doc["model"] = _compact({
            "requested": _stated(spec.model.requested),
            "provider": _known(spec.model.provider),
            "resolved": _known(spec.model.resolved),
            "parameters": block(spec.model.parameters, "model.parameters.*"),
        }) or None
        if spec.instructions.text is not None:
            doc["instructions"] = text(spec.instructions.text, "instructions")
        elif spec.instructions.hash:
            doc["instructions"] = {"hash": spec.instructions.hash}
        else:
            doc["instructions"] = UNKNOWN

        doc["tools"] = [_compact({
            "ref": use.ref,
            "name": use.name,
            "description": text(use.description, f"tools[{use.ref}].description"),
            "scope": _stated(use.scope),
            "reversible": _stated(use.reversible),
            "requires_confirmation": _stated(use.requires_confirmation),
            "input_schema": block(use.input_schema, f"tools[{use.ref}].input_schema"),
            "output_schema": block(use.output_schema, f"tools[{use.ref}].output_schema"),
        }) for use in spec.tools]
        doc["knowledge"] = [_compact({
            "ref": item.ref,
            "name": item.name,
            "description": text(item.description, f"knowledge[{item.ref}].description"),
            "type": item.type,
            "scope": _stated(item.scope),
        }) for item in spec.knowledge]

        permissions = spec.permissions
        doc["permissions"] = _compact({
            "identity": _stated(permissions.identity),
            "principal": _stated(permissions.principal),
            "grants": [{"target": g.target, "privilege": g.privilege, "scope": g.scope.value} for g in permissions.grants],
            "access": [{"principal": a.principal, "principal_type": a.principal_type, "level": a.level} for a in permissions.access],
        })
        doc["bindings"] = record.bindings or None
        doc["extensions"] = {
            platform: block(values, f"extensions.{platform}.*") for platform, values in spec.extensions.items()
        } or None
        return Written(document=_compact(doc), notes=notes)

    def dump(self, document: dict[str, Any]) -> str:
        return HEADER + yaml_text(document)


TEMPLATE = """\
# Marshal file: a hand-written definition for an agent no reader covers.
# Field reference: docs/formats/marshal-file.md
#
# Values here are recorded as attested by the person below, not as declared by
# the platform. Write only what you have checked. Leave a field out, or write
# `unknown`, when you can't confirm it: the ledger never guesses.
marshal: agent/v0
id: {agent}              # stable identity, the same in every environment
platform: {platform}     # where the agent runs, such as an in-house platform's name
attested_by: unknown     # who checked these values: a person or team address
attested_at: unknown     # when, as YYYY-MM-DD

display_name: unknown
description: unknown

model:
  requested: unknown     # the model or alias the agent asks for
  provider: unknown      # openai, anthropic, azure-openai, bedrock, ...
  # parameters: {{temperature: 0.2}}

# The system prompt, as a block. If the text can't be shared, give its digest instead:
# instructions: {{hash: "sha256:<64 hex digits>"}}
instructions: unknown

# One entry per tool. ref is <kind>/<identifier>; a bare name becomes tool/<platform>.<name>.
# scope: read | write | execute | admin. reversible and requires_confirmation: true | false | unknown.
tools: []
#  - ref: tool/{platform}.orders.lookup
#    name: lookup_order
#    description: Fetch an order by number.
#    scope: read
#    reversible: true
#    requires_confirmation: false

# Data the agent reads: indexes, tables, document stores.
knowledge: []
#  - ref: knowledge/{platform}.refund-policy
#    name: Refund policy
#    type: document
#    scope: read

permissions:
  # service_principal | on_behalf_of_user | shared_credential | mixed
  identity: unknown
  principal: unknown     # the account the agent acts as
  grants: []
  #  - target: store/postgres.payments.refunds
  #    privilege: INSERT
  #    scope: write
  access: []
  #  - principal: support-agents
  #    principal_type: group      # user | group | service_principal | unknown
  #    level: use

# Environment-bound values (endpoints, environment variable names): recorded,
# not part of the version hash. Never put a credential here.
bindings: {{}}

# Fields with no ledger equivalent, keyed by platform. Kept as written.
extensions: {{}}
"""


def template(agent: str, platform: str) -> str:
    """A commented Marshal file to fill in, with every scored field unknown."""
    return TEMPLATE.format(agent=agent, platform=platform)
