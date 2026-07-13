# API reference

The hand-written public surface, rendered from its docstrings.  It is small
on purpose: the SDK's bulk — the typed cursors of the design DSL, their
keyword arguments and the enums behind them — is **generated**, and so is its
reference ({doc}`../vocabulary/index`).

| If you are looking for… | Go to |
| --- | --- |
| the clients, the nodes, navigation | {doc}`client` |
| the design DSL: roots, `Cursor`, push results | {doc}`design` |
| **the fields of a maker** (`.bd()`, `.epg()`, …) | {doc}`../vocabulary/index` |
| reads: query builder and filters | {doc}`query` |
| what a failure raises | {doc}`exceptions` |
| the model contract | {doc}`models` |
| sessions, retries, the transport protocols | {doc}`transport` |
| diffing and response parsing | {doc}`utils` |

```{toctree}
:maxdepth: 2

niwaki
client
design
query
exceptions
models
transport
utils
```
