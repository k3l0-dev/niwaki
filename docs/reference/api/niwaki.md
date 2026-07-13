# The `niwaki` package

```{eval-rst}
.. automodule:: niwaki
   :no-members:
```

Everything below is importable straight from the package root:

| Import | What it is |
| --- | --- |
| `Niwaki`, `AsyncNiwaki` | the clients — {doc}`client` |
| `NiwakiNode`, `AsyncNiwakiNode` | DN-scoped handles — {doc}`client` |
| `RetryConfig` | the retry policy — {doc}`transport` |
| `tenant`, `infra`, `fabric`, `controller`, `design` | the design roots — {doc}`design` |

Sub-packages: `niwaki.design` (write), `niwaki.query` (read),
`niwaki.models` (typed ACI classes), `niwaki.exceptions`,
`niwaki.transport`, `niwaki.utils`.

```{eval-rst}
.. autodata:: niwaki.__version__
   :no-value:
```
