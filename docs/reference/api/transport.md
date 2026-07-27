# Transport

```{eval-rst}
.. automodule:: niwaki.transport
   :no-members:
```

## The transport boundary

These protocols document what the upper layers ask of a session: the
design engine's wave runner consumes the async writer protocol; the facade
and the query builders drive the concrete sessions directly. For testing,
fake the HTTP layer (as niwaki's own suite does) and use the protocols as
shape contracts for engine-level stubs ({doc}`../../guide/testing`).

```{eval-rst}
.. autoclass:: niwaki.transport.MoWriter

.. autoclass:: niwaki.transport.MoReader

.. autoclass:: niwaki.transport.AsyncMoWriter

.. autoclass:: niwaki.transport.AsyncMoReader
```

## Sessions

Authentication, proactive token refresh, retries and transparent pagination
live here.  The clients construct and close a session for you — reach for
these classes only when you want a transport without the facade.

```{eval-rst}
.. autoclass:: niwaki.transport.ApicSession

.. autoclass:: niwaki.transport.AsyncApicSession
```

## Retry policy

```{eval-rst}
.. autoclass:: niwaki.transport.RetryConfig
```
