# Observe and verify a fabric

**Problem** — answer "what state is this fabric in?" without changing it:
inventory, faults, and the health of what you own.  This is the pure read side —
the facade and the query builder — and for a large fabric, the async client fans
the reads out into one wall-clock latency.

To have something to look at, the page first pushes a slice of the `commerce`
deployment, then audits it:

```python
from niwaki import Niwaki
from niwaki.design import tenant

aci = Niwaki.connect("https://apic.example.com", "admin", "secret")

config = tenant("commerce")
config.vrf("prod")
for name, gw in {"bd-web": "10.30.10.1/24", "bd-app": "10.30.20.1/24"}.items():
    config.bd(name, unicast_routing=True).bind(vrf="prod").subnet(gw)
config.app("storefront").epg("web").bind(bd="bd-web")
config.push(aci)
```

The `with` form closes the session for you; `connect()` is used here so the
rest of the page can share one client — see {doc}`../guide/connection`.

## Verify what you deployed

Scope the query to the tenant subtree and let the APIC filter — you do not:

```python
bds = aci.tenant("commerce").query("fvBD").fetch()
assert {b.name for b in bds} == {"bd-web", "bd-app"}

web = aci.tenant("commerce").bd("bd-web").read()
assert web.unicast_routing is True
```

`aci.tenant(...).query(...)` bounds the read to a subtree; `aci.query(...)`
targets a class fabric-wide.

## Inventory and faults

Operational classes are queryable **by name** — no imports, no generated models.
`naming_only()` trims the payload to identity attributes, the right call for
inventory sweeps on big fabrics:

```python
nodes = aci.query("topSystem").naming_only().fetch()
critical = aci.query("faultInst").where(severity="critical").fetch()

print(f"{len(nodes)} nodes, {len(critical)} critical faults")
```

`where()` speaks to the APIC, so it uses **wire** attribute names (`severity`,
`lcOwn`, `arpFlood`), not the pythonic field names.

## Health of what you own

Enrichment flags hang health and fault children off each returned object in the
same round trip:

```python
enriched = aci.tenant("commerce").query("fvBD").with_health().with_faults().fetch()
for bd in enriched:
    print(bd.name)
```

## The async fan-out

An audit is the textbook `gather()` case — independent reads collapsed into one
latency ({doc}`../guide/async`):

```python
import asyncio

from niwaki import AsyncNiwaki


async def report() -> dict[str, int]:
    async with AsyncNiwaki("https://apic.example.com", "admin", "secret") as aci:
        tenants, bds, epgs, faults = await aci.gather(
            aci.query("fvTenant").fetch(),
            aci.query("fvBD").fetch(),
            aci.query("fvAEPg").fetch(),
            aci.query("faultInst").fetch(),
        )
        return {
            "tenants": len(tenants),
            "bds": len(bds),
            "epgs": len(epgs),
            "faults": len(faults),
        }


summary = asyncio.run(report())
assert summary["bds"] == 2
print(summary)
```

## Streaming the big ones

Endpoint tables and fault histories can be six digits; stream instead of holding
the whole list in memory:

```python
seen = 0
for _bd in aci.query("fvBD").stream():
    seen += 1
assert seen == 2
```

## Variations & pitfalls

- **Audit is not a drift check** — for objects a *design* owns, the design's own
  `plan` is the sharper tool ({doc}`day-2-changes`).  The query side is for what
  you do *not* own: faults, endpoints, operational state.
- **Wire names in filters** — a filter string goes to the APIC verbatim, so
  `where(arpFlood=True)`, not `where(arp_flooding=True)`
  ({doc}`../guide/models`).
- **Rate-respect on shared fabrics** — the async client's `max_concurrent`
  (default 10) is the polite ceiling; audits rarely need more.
- **Deletion is the one imperative** — the facade reads, but it can also
  `delete()` a DN and its subtree, because a design never removes what it does
  not declare ({doc}`../guide/observing`).
