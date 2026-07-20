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

## Subscribing (push)

`Query.subscribe()`/`AsyncQuery.subscribe()` open a live push stream instead
of a one-off read — see {doc}`../../guide/subscribing`.

```{eval-rst}
.. autoclass:: niwaki.query.Subscription
   :members:

.. autoclass:: niwaki.query.AsyncSubscription
   :members:

.. autoclass:: niwaki.query.SubscriptionEvent
   :members:

.. autoclass:: niwaki.query.EventKind
   :members:

.. autoclass:: niwaki.query.SubscriptionInfo
   :members:
```

## Response-subtree facets

`include_subtree` embeds one or more of these facets into the response
(`rsp-subtree-include`); `with_faults`, `with_health` and `with_stats` are
shortcuts for the common ones.

```{eval-rst}
.. autoclass:: niwaki.query.SubtreeInclude
   :members:
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
.. autofunction:: niwaki.query.anybit
.. autofunction:: niwaki.query.allbit
.. autofunction:: niwaki.query.xor
.. autofunction:: niwaki.query.raw
```

## Value wrappers

Smart values for `where(...)` — a list means membership, a `*` means wildcard, a
`set` stays bitmask equality; these wrappers make the intent explicit.

```{eval-rst}
.. autofunction:: niwaki.query.any_of
.. autofunction:: niwaki.query.like
.. autofunction:: niwaki.query.between
```
