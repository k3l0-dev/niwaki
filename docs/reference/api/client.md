# Clients and nodes

`Niwaki` and `AsyncNiwaki` own the session; `NiwakiNode` / `AsyncNiwakiNode`
are the DN-scoped handles they hand out.  The clients **observe** — writing
goes through the design DSL ({doc}`design`).

## Clients

```{eval-rst}
.. autoclass:: niwaki.Niwaki

.. autoclass:: niwaki.AsyncNiwaki
```

## Nodes

```{eval-rst}
.. autoclass:: niwaki.NiwakiNode

.. autoclass:: niwaki.AsyncNiwakiNode
```

## Subscription management

`aci.subscriptions` — bulk introspection/refresh/stop over every
subscription open on the session's shared WebSocket; see
{doc}`../../guide/subscribing`.

```{eval-rst}
.. autoclass:: niwaki.facade.SubscriptionManager
   :members:

.. autoclass:: niwaki.facade.AsyncSubscriptionManager
   :members:
```

## Vocabulary navigation

Nodes — and the clients themselves — resolve **operator vocabulary** into
typed child handles: `aci.tenant("prod").bd("web")` is not a hardcoded method
chain, it is resolved against the APIC containment model at each step.

```{eval-rst}
.. automethod:: niwaki.NiwakiNode.__getattr__
```

## Retry policy

Retries are configured on the client with {class}`~niwaki.transport.RetryConfig`
(documented with the {doc}`transport <transport>` layer that applies it).
