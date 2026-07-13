# Static paths — attaching an EPG to ports

**Problem** — the `web` EPG must actually reach the wire: tagged VLAN 120 on
a single server port, and VLAN 121 on the ESXi vPC from the previous
recipe.

## The design

A static path is the one relation whose target lives *outside* the `uni`
subtree — the path DN names physical topology.  It is therefore a **maker
with a literal DN**, not a `bind()`:

```python
from niwaki import Niwaki
from niwaki.design import tenant

config = tenant("shop")
epg = config.app("storefront").epg("web")

# The EPG joins the physical domain (declared in the access-policy recipe,
# so referenced here by DN — it lives outside this design)
epg.bind_dn(domain="uni/phys-prod-phys")

# Single access port: node 101, eth1/13
epg.static_path(
    "topology/pod-1/paths-101/pathep-[eth1/13]",
    encap="vlan-120",
    mode="regular",
    deployment_immediacy="immediate",
)

# vPC: the pathep names the policy group, paths carry both node ids
epg.static_path(
    "topology/pod-1/protpaths-101-102/pathep-[esxi-vpc]",
    encap="vlan-121",
    deployment_immediacy="immediate",
)
```

## Plan, push, verify

```python
aci = Niwaki.connect("https://apic.example.com", "admin", "secret")

plan = config.push(aci, mode="plan")
print(plan.creates)
config.push(aci)

paths = aci.query("fvRsPathAtt").fetch()
assert sorted(p.encap for p in paths) == ["vlan-120", "vlan-121"]
```

The `with` form closes the session for you; `connect()` is used here so the
rest of the page can share one client — see {doc}`../guide/connection`.

## Reading the path DN

| Segment | Meaning |
| --- | --- |
| `topology/pod-1` | pod |
| `paths-101` | single leaf 101 — access port or port channel |
| `protpaths-101-102` | vPC protection pair 101+102 |
| `pathep-[eth1/13]` | the interface… |
| `pathep-[esxi-vpc]` | …or the policy-group name for (v)PC paths |

## Variations & pitfalls

- **`mode`** — `regular` tags the VLAN (trunk); `native` sends it untagged
  with a native VLAN; `untagged` is access-port style.  Default is
  `regular`; hypervisor uplinks almost always want tagged.
- **Encap must be in the pool** — VLAN 120/121 resolve only if the domain's
  pool covers them (`vlan-100`–`vlan-199` here); the APIC rejects the path
  otherwise, at push time, per path.
- **vPC prerequisites** — the `protpaths` DN implies the protection pair
  *and* the `esxi-vpc` policy group already exist
  ({doc}`access-policies-vpc`).
- **Scale hint** — tens of paths per EPG are fine in one design; for
  hundreds, generate the design in a loop from your source of truth (it is
  plain Python) and prefer `staged` mode for progress granularity.
