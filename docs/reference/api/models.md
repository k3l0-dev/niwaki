# Models

All 2,222 generated classes share one contract — data and validation, never
write logic.  Import them by package alias:

```python
from niwaki.models.fv.fvBD import fvBD
from niwaki.models.vz.vzBrCP import vzBrCP

bd = fvBD(name="web", unicast_routing=True)   # validated at construction
bd.rn                                          # "BD-web"
bd.to_apic()                                   # wire payload, ACI attribute names
```

Field names are human-readable (`arp_flooding`), with the ACI wire name as a
Pydantic alias (`arpFlood`) — both parse on input, the wire name is always
emitted.  558 field enums live under `niwaki.models._generated.enums`.

```{eval-rst}
.. autoclass:: niwaki.models.base.ManagedObject
   :members:
```
