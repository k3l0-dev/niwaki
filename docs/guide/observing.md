# Observing the fabric

The facade is the SDK's read side: navigation in operator vocabulary, typed
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

The `with` form closes the session for you; `connect()` is used here so the
rest of the page can share one client — see {doc}`connection`.

## Vocabulary navigation

Every node is a DN-scoped handle.  Navigate with the same vocabulary the GUI
tree uses; the DN is computed for you:

```python
bd = aci.tenant("prod").bd("web")           # NiwakiNode at uni/tn-prod/BD-web
assert bd.dn == "uni/tn-prod/BD-web"
mo = bd.read()                              # typed fvBD instance
assert mo.unicast_routing is True           # human-readable field names
```

`aci.node(dn, cls)` reaches any explicit DN; `.mo(Class, **naming)` descends
one level for classes outside the curated vocabulary.

## The query builder

{meth}`~niwaki.Niwaki.query` targets a class fabric-wide; `node.query(...)`
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

## Deleting

Deletion is the **one imperative operation** on the facade — a design never
removes what it does not declare, so removals are always an explicit act:

```python
retired = tenant("prod-old")
retired.push(aci)                    # a tenant to retire

aci.tenant("prod-old").delete()      # removes the object AND its subtree
```

Deleting a DN removes the whole subtree beneath it, exactly as in the GUI.
A `delete()` on a DN that does not exist raises
{class}`~niwaki.exceptions.NotFoundError` — deletion is never silently a
no-op:

```python
import pytest

from niwaki.exceptions import NotFoundError

with pytest.raises(NotFoundError):
    aci.tenant("prod-old").read()    # gone, subtree included
```

Day-2 removal of a single child (a subnet, a static path) is the same
gesture one level deeper: navigate to it, `delete()` it, and keep the design
in sync by no longer declaring it.

## Async

{class}`~niwaki.AsyncNiwaki` mirrors everything on this page —
accumulators stay synchronous, executors become awaitable, and `gather()`
runs reads concurrently.  See {doc}`async` for the concurrency model.

## Next steps

- {doc}`models` — what the typed reads give you back
- {doc}`async` — the same API, concurrent
- {doc}`../cookbook/fabric-audit` — the read side at work
