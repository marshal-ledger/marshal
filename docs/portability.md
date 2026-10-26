# Portability

```console
$ marshal port finance/invoice_agent --to copilot-studio
$ marshal port path/to/bundle --to all
$ marshal collect . --target prod --event proposed --portability bedrock --portability copilot-studio
```

`marshal port` reports what it would take to move an agent to another platform. The agent comes from the store (`AGENT[@selector]`) or from a path to read.

```
agent_template/BillingAgent → Microsoft Copilot Studio

Portability: 67 / 100
  Platform limitations (copilot-studio):
    ~ identity service_principal: other identities; give each connection the maker's credentials or the end user's
    ~ tool SavingsPlanAgent: other tools; expose the tool as an API and add it as a custom connector
  Agent-specific issues:
    ? tool SavingsPlanAgent: scope unknown in the source
Fidelity: 6 lossless · 13 dropped · 2 defaulted
```

## How the score is computed

The report keeps two parts separate:

1. **Platform limitations**, from the target's capability matrix. The matrix says whether the target supports each kind of tool, knowledge source, identity mode and model provider natively (`native`), only after rework (`adapt`, with advice), or not at all (`none`).
2. **Agent-specific issues**, from converting the agent with the target's writer, where Marshal has one: fields that don't carry over for reasons the matrix doesn't cover, such as a tool name that isn't valid on the target.

Each element of the agent has a weight: identity 3, each tool 3, model 2, instructions 2, each knowledge source 2, and each tool that requires human confirmation 1. A limitation deducts its element's whole weight for `none` and half for `adapt`. An agent-specific issue deducts a quarter. The score is the share of weight left, out of 100.

Unknown values in the source, such as a tool whose scope isn't declared, are listed with `?` but don't count against the score. A low completeness score is the better signal for those.

An agent assessed against the platform it already runs on isn't scored. It's reported as the current platform, because rewriting it in its own format can lose detail that moving it wouldn't.

## The capability matrix

One YAML file per platform in [`src/marshal_ledger/capabilities/`](../src/marshal_ledger/capabilities/): Copilot Studio, Bedrock Agents, Agentforce, Google ADK on Agent Engine, and Databricks. Rules match entity refs (for tools and knowledge) or values (for identity kinds and model providers). The first match wins, and every list ends with a `"*"` default.

The files are meant for contributions. Each cites the platform documentation it is based on, and a test checks that every rule has a valid level, every non-native rule has advice, and every list has a default. See the matrix [README](../src/marshal_ledger/capabilities/README.md).
