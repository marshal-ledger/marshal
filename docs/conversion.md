# Conversion and fidelity

```console
$ marshal convert agent.adl.yaml --to agent-spec > agent.agentspec.yaml
$ marshal convert path/to/bundle --target prod --to adl --out converted/ --report fidelity.json
```

`marshal convert` reads agents in any supported format and writes them in another, through the ledger model. With one agent, the document goes to stdout. With several, `--out` writes one file per agent. The fidelity report goes to stderr, and also to `--report` as JSON.

Every reader's output can be written by every writer:

| Readers | Writers |
|---|---|
| `adl`, `agent-spec`, `oasf`, `cloudformation`, `copilot-studio`, `agentforce`, `adk-config`, `databricks-asset-bundle`, `python`, `marshal-file` | `adl`, `agent-spec`, `oasf`, `cloudformation`, `copilot-studio`, `agentforce`, `adk-config`, `marshal-file` |

Copilot Studio and ADK Agent Config definitions are written as files in a folder, so converting to them needs `--out`.

Converting to `marshal-file` gives a person a hand-written definition to check and maintain. Fields the record doesn't know are written as `unknown`. See [Marshal file](formats/marshal-file.md).

## How fidelity is computed

The converter doesn't estimate what survives. It writes the target document, reads it back with that format's reader, and compares the two ledger records field by field:

| Outcome | Meaning |
|---|---|
| lossless | The field reads back with the same value |
| approximated | It reads back with a different value, for example a tool name made valid for the target |
| dropped | It doesn't read back. The target can't express it |
| defaulted | It reads back although the source had no value, because the target requires one and the writer filled it in |

Each non-lossless field carries a reason, either from the writer's declared loss table or from a note the writer made during the conversion.

```
https://agents.acme.example/invoice-processor → agent-spec
  Fidelity: 15 lossless · 1 approximated · 23 dropped · 3 defaulted
    ~ tools[tool/adl.invoice-processor.search_invoices].input_schema: … (Agent Spec marks a parameter optional only by a default; …)
    - tools[tool/adl.invoice-processor.search_invoices].scope (Agent Spec states a tool's effect only through a remote tool's HTTP method)
    - permissions.grants[store/file./data/audit/** read_write] (Agent Spec does not describe identity or permissions)
    - extensions.adl.* (13 fields; adl-specific; no Agent Spec equivalent)
    + extensions.agent-spec.llm_config (Agent Spec requires an LLM config; one was built from the model fields)
```

## Identity across formats

A converted agent keeps its ledger identity: its agent id, its platform, and its tool and knowledge refs. Where the target format can't name these itself, the writer records them in a Marshal extension block under the `io.github.marshal-ledger` key: ADL `extensions`, Agent Spec `metadata`, or OASF `annotations`. That way a document converted from Databricks to ADL reads back as the same Databricks agent. Native documents that already carry their own identity get no Marshal block.

## Testing guarantees

Golden tests run against real, public definitions in each format:

- **Round trip.** Every ADL, Agent Spec, OASF, Agentforce and Marshal file fixture is read, written in its own format, and read back. The result must be lossless, with the same version hash. CloudFormation round trips are held to "every loss explained" instead, because a template also defines roles and executors that the writer references rather than generates.
- **Every loss explained.** Every fixture, in every format, converts to every writer. Each lost or defaulted field must have a reason, and the agent must keep its identity.
- **Schema validity.** Written ADL and Agent Spec documents, and every written Copilot Studio file with a `kind`, must validate against the formats' published schemas.
