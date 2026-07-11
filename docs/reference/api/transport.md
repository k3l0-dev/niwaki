# Transport boundary

The facade and the design push engine depend on structural protocols, not on
the concrete sessions — any conforming object is a valid transport (test
stubs included).  Sessions themselves are managed by the clients; you rarely
touch them directly.

```{eval-rst}
.. automodule:: niwaki.transport._protocols
   :members:
```
