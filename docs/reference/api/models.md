# Models

The 2,222 generated classes share one contract — data and validation, never
write logic.  Import them by package alias:

```python
from niwaki.models.fv.fvBD import fvBD

bd = fvBD(name="web", unicast_routing=True)   # validated at construction
bd.rn                                          # "BD-web"
bd.to_apic()                                   # wire payload, ACI attribute names
```

**Where the fields are documented.**  Every class reachable through the
design DSL has its full attribute table — name, wire alias, type, allowed
values, default and Cisco's own definition — in the generated
{doc}`DSL reference <../vocabulary/index>`, one page per position.  For the
other generated classes, the same descriptions live in the code and in your
IDE (`fvBD.model_fields["arp_flooding"].description`); this site does not
paginate all 2,222 of them.

Conventions and day-to-day usage — readable names vs wire aliases, enums,
validation, the `.mo()` escape hatch: {doc}`../../guide/models`.

## The base class

```{eval-rst}
.. autoclass:: niwaki.models.ManagedObject
```

## The registry

```{eval-rst}
.. autodata:: niwaki.models.base.REGISTRY
   :no-value:
```
