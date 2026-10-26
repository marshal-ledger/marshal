# Capability matrix

One file per platform, saying what an agent on that platform can use. `marshal port` reads these files to find platform limitations: what an agent would have to give up or rework to move there.

Each rule matches an entity ref (tools, knowledge) or a value (identity kinds, model providers) and gives a level:

| Level | Meaning |
|---|---|
| `native` | The platform supports it directly |
| `adapt` | It works after rework; `advice` says how |
| `none` | The platform can't support it |

Rules are tried in order and the first match wins. Patterns use `*` as the only wildcard. End each list with a `"*"` rule as the default.

To correct or extend a file, change the rule, cite the platform documentation in `sources`, and run `pytest`.
