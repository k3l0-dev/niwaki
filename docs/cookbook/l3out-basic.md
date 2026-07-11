# A basic L3Out shell

**Problem** — the `prod` VRF needs a way out of the fabric.  This recipe
builds the L3Out shell — the L3Out itself, its VRF and domain wiring — with
the curated vocabulary, then goes below the waterline (node and interface
profiles) with the escape hatches, honestly flagged.

## The curated part

```python
from niwaki import Niwaki
from niwaki.design import tenant

config = tenant("shop")
config.vrf("prod")
config.l3_dom("wan-dom")

l3out = config.l3out("wan").bind(vrf="prod")
```

`l3out.bind(vrf=...)` places the relation where ACI expects it
(`l3extRsEctx` on the L3Out side); the same edge is reachable from the VRF
cursor as `vrf.bind(l3out="wan")` — one declaration either way.

## Below the waterline: `.mo()`

Node profiles, interface profiles and BGP peers are **not curated yet** —
they remain reachable through `.mo()`, with containment still validated
against the schema:

```python
from niwaki.models.l3ext.l3extLNodeP import l3extLNodeP
from niwaki.models.l3ext.l3extLIfP import l3extLIfP

nodes = l3out.mo(l3extLNodeP, name="border-leaves")
nodes.mo(l3extLIfP, name="uplinks")
```

The `.mo()` blocks read as what they are: raw ACI classes, no operator
verbatim.  If you build L3Outs routinely, that is a vocabulary gap worth a
[vocabulary request](https://github.com/k3l0-dev/niwaki/issues/new/choose)
— curation grows by demand.

## Plan, push, verify

```python
aci = Niwaki.connect("https://apic.example.com", "admin", "secret")

plan = config.push(aci, mode="plan")
print(f"{len(plan.creates)} objects")
config.push(aci)

outs = aci.query("l3extOut").fetch()
assert [o.name for o in outs] == ["wan"]
```

## Variations & pitfalls

- **The domain still needs encap** — `wan-dom` wants a
  `bind(vlan_pool=...)` to a pool covering the uplink VLANs, exactly as in
  {doc}`access-policies-vpc`.
- **External EPGs govern reachability** — an L3Out without an
  `l3extInstP` (external EPG) and its subnets forwards nothing; that class
  is also `.mo()` territory today.
- **Advertising BD subnets** — a BD whose subnet must be advertised needs
  `bd.bind(l3out="wan")` *and* the subnet scoped `public`; keep both in the
  same design so the intent reviews as one change.
