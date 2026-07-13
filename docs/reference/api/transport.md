# Transport

```{eval-rst}
.. automodule:: niwaki.transport
   :no-members:
```

## The transport boundary

The facade and the push engine depend on these structural protocols, never on
the concrete sessions — any conforming object is a valid transport, which is
how you test your automation without a fabric ({doc}`../../guide/testing`).

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
