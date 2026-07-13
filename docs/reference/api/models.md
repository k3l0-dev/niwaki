# Models

All 2,222 generated classes share one contract — data and validation, never
write logic.  Import them by package alias:

```python
from niwaki.models.fv.fvBD import fvBD

bd = fvBD(name="web", unicast_routing=True)   # validated at construction
bd.rn                                          # "BD-web"
bd.to_apic()                                   # wire payload, ACI attribute names
```

Conventions and day-to-day usage — readable names vs wire aliases, enums,
validation, the `.mo()` escape hatch: {doc}`../../guide/models`.

```{eval-rst}
.. autoclass:: niwaki.models.base.ManagedObject
   :members:
```
