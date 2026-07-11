# Observing the fabric

The facade is the SDK's read side: navigation in operator jargon, typed
reads, the query builder, and deletion.  It never configures — writing is the
design DSL's job.

The examples below observe a small tenant pushed by its design:

```python
from niwaki import Niwaki
from niwaki.design import tenant

config = tenant("prod")
config.bd("web", unicast_routing=True).bind(vrf="main")
config.vrf("main")

aci = Niwaki.connect("https://apic.example.com", "admin", "secret")
config.push(aci)
```

## Jargon navigation

Every node is a DN-scoped handle.  Navigate with the same vocabulary the GUI
tree uses; the DN is computed for you:

```python
bd = aci.tenant("prod").bd("web")           # NiwakiNode at uni/tn-prod/BD-web
assert bd.dn == "uni/tn-prod/BD-web"
mo = bd.read()                              # typed fvBD instance
assert mo.unicast_routing is True           # human-readable field names
```

`aci.node(dn, cls)` reaches any explicit DN; `.mo(Class, **naming)` descends
one level for classes outside the curated jargon.  Deletion is the one
imperative operation that stays here: `aci.tenant("old").delete()`.

## The query builder

{meth}`~niwaki.facade.Niwaki.query` targets a class fabric-wide; `node.query(...)`
scopes it to a subtree.  Accumulate, then execute:

```python
from niwaki.models.fv.fvBD import fvBD

# Filters, scoping, counting — filters address the APIC attribute
# names (the wire side: arpFlood, not arp_flooding)
bds = aci.query(fvBD).where(arpFlood=True).under("uni/tn-prod").fetch()
n   = aci.tenant("prod").query(fvBD).count()

# Filter expressions when kwargs are not enough — qualify the
# property (or build with cls_name="fvAEPg")
from niwaki.query import gt, or_, eq
epgs = aci.query("fvAEPg").where(
    or_(eq("fvAEPg.name", "web"), gt("fvAEPg.pcTag", "10000"))
).fetch()

# Enrichment
unhealthy = aci.query(fvBD).with_health().with_faults().fetch()

# Streaming for large result sets
for bd in aci.query(fvBD).stream():
    ...
```

Any of the ~15,000 APIC classes is queryable **by name** — including
read-only operational classes outside the generated set:

```python
nodes = aci.query("topSystem").naming_only().fetch()
```

`build()` returns the URL and parameters without executing — the read-side
mirror of `to_payload()`.

## Async

{class}`~niwaki.facade.AsyncNiwaki` mirrors the sync API; accumulators stay
synchronous, executors are awaitable, and `gather()` runs reads concurrently
under a TaskGroup:

```python
import asyncio

from niwaki import AsyncNiwaki
from niwaki.models.fv.fvTenant import fvTenant


async def snapshot() -> None:
    async with AsyncNiwaki("https://apic.example.com", "admin", "secret") as aci:
        tenants, bd = await aci.gather(
            aci.query(fvTenant).fetch(),
            aci.tenant("prod").bd("web").read(),
        )
        await config.push(aci)    # the same design — async-ready too


asyncio.run(snapshot())
```
