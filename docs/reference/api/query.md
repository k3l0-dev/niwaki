# Reading — queries and filters

```{eval-rst}
.. automodule:: niwaki.query
   :no-members:
```

## Builders

Accumulators are synchronous on both builders; only the executors differ
(`fetch`/`first`/`count`/`stream` — awaitable on `AsyncQuery`).

```{eval-rst}
.. autoclass:: niwaki.query.Query
   :inherited-members:

.. autoclass:: niwaki.query.AsyncQuery
   :inherited-members:
```

## Filter expressions

Filter expressions compose with the `&`, `|` and `~` operators — the
methods below are what those operators call.

```{eval-rst}
.. autoclass:: niwaki.query.FilterExpr

.. automethod:: niwaki.query.FilterExpr.__and__
.. automethod:: niwaki.query.FilterExpr.__or__
.. automethod:: niwaki.query.FilterExpr.__invert__
```

## Filter functions

```{eval-rst}
.. autofunction:: niwaki.query.eq
.. autofunction:: niwaki.query.ne
.. autofunction:: niwaki.query.gt
.. autofunction:: niwaki.query.ge
.. autofunction:: niwaki.query.lt
.. autofunction:: niwaki.query.le
.. autofunction:: niwaki.query.bw
.. autofunction:: niwaki.query.wcard
.. autofunction:: niwaki.query.and_
.. autofunction:: niwaki.query.or_
.. autofunction:: niwaki.query.not_
```
