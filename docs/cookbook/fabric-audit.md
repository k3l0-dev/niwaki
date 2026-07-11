# Auditing a fabric — read-only reporting

**Problem** — answer "what state is this fabric in?" without touching it:
inventory, faults, and the health of what you own.  Pure read side — the
facade and the query builder, fanned out with the async client when the
fabric is large.

## Inventory and faults

Operational classes are queryable by name — no imports, no generated
models needed:

```python
from niwaki import Niwaki

aci = Niwaki.connect("https://apic.example.com", "admin", "secret")

nodes = aci.query("topSystem").naming_only().fetch()
critical = aci.query("faultInst").where(severity="critical").fetch()

print(f"{len(nodes)} nodes, {len(critical)} critical faults")
```

`naming_only()` trims the payload to identity attributes — the right call
for inventory sweeps on big fabrics.

## Health of what you own

Enrichment flags hang health and fault children off each returned object:

```python
bds = aci.tenant("shop").query("fvBD").with_health().with_faults().fetch()
for bd in bds:
    print(bd.name)
```

Scoping first (`aci.tenant("shop")`) keeps the query subtree-bound: the
APIC filters, you do not.

## The async fan-out

An audit is the textbook `gather()` case — independent reads, one wall-clock
latency ({doc}`../guide/async`):

```python
import asyncio

from niwaki import AsyncNiwaki


async def report() -> dict[str, int]:
    async with AsyncNiwaki("https://apic.example.com", "admin", "secret") as aci:
        nodes, faults, tenants, paths = await aci.gather(
            aci.query("topSystem").naming_only().fetch(),
            aci.query("faultInst").fetch(),
            aci.query("fvTenant").fetch(),
            aci.query("fvRsPathAtt").fetch(),
        )
        return {
            "nodes": len(nodes),
            "faults": len(faults),
            "tenants": len(tenants),
            "static_paths": len(paths),
        }


summary = asyncio.run(report())
print(summary)
```

## Streaming the big ones

Endpoint tables and fault histories can be six digits; stream instead of
holding the list:

```python
seen = 0
for _ep in aci.query("fvCEp").stream():
    seen += 1
print(f"{seen} endpoints")
```

## Variations & pitfalls

- **Wire names in filters** — `where()` speaks to the APIC, so it uses wire
  attribute names (`severity`, `lcOwn`, `arpFlood`), not the pythonic field
  names ({doc}`../guide/models`).
- **Audit ≠ drift check** — for objects a design owns, the design's own
  `plan` is the sharper tool ({doc}`vlan-migration`); the query side is for
  what you do *not* own — faults, endpoints, operational state.
- **Rate-respect on shared fabrics** — the async client's `max_concurrent`
  (default 10) is the polite ceiling; audits do not need more.
