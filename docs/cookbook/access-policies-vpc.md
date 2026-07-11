# Access policies for a vPC server rack

**Problem** — wire a new rack: ESXi hosts dual-homed to leaves 101–102 over
a vPC.  In ACI terms that is the entire access-policy chain — interface
policies, a VLAN pool, a physical domain, an AAEP, a vPC policy group,
interface and switch profiles — plus the vPC protection pair on the fabric
side.  It is the canonical "twelve GUI screens" task; here it is one design.

## The design

```python
from niwaki import Niwaki
from niwaki.design import design

config = design()
inf = config.infra()

# Interface policies — reusable, so named after behaviour, not location
inf.cdp_policy("cdp-on", admin_state="enabled")
inf.lldp_policy("lldp-on", receive_state="enabled", transmit_state="enabled")
inf.lacp_policy("lacp-active", mode="active")

# Encap: pool → physical domain → AAEP
inf.vlan_pool("prod-static", "static").range("vlan-100", "vlan-199")
config.phys_dom("prod-phys").bind(vlan_pool="prod-static")
inf.aaep("prod-aaep").bind(domain="prod-phys")

# The vPC policy group ties the interface policies and the AAEP together
policies = inf.func_profile()
policies.port_channel("esxi-vpc", link_aggregation_type="node").bind(
    aaep="prod-aaep", cdp="cdp-on", lldp="lldp-on", lacp="lacp-active"
)

# Interface profile: which ports…
ports = inf.access_port_profile("rack12-ports")
selector = ports.port_selector("esxi-uplinks", "range")
selector.port_block("blk1", from_port_id="13", to_port_id="14")
selector.bind(policy_group="esxi-vpc")

# …on which switches
leaves = inf.leaf_profile("rack12-leaves")
leaves.leaf_selector("pair", "range").node_block(
    "blk1", from_node_id="101", to_node_id="102"
)
leaves.bind(interface_profile="rack12-ports")

# Fabric side: the explicit vPC protection pair
pair = config.fabric().vpc_protection().vpc_pair("101-102", logical_pair_id="12")
pair.node("101")
pair.node("102")
```

Note the shape of the chain: `port_selector.bind(policy_group=...)` targets
an *abstract* class, so the same alias would accept an `access_group` for
single-homed servers; `phys_dom` and `aaep` live at their real positions
(`uni` and `uni/infra`) — one `design()` covers both domains plus `fabric`.

## Plan, push

```python
aci = Niwaki.connect("https://apic.example.com", "admin", "secret")

plan = config.push(aci, mode="plan")
print(f"infra: {len(plan.creates)} creates")

report = config.push(aci, mode="staged")   # per-object waves, parents first
```

`staged` suits access policies on brownfield fabrics: each object lands in
its own request, so a conflict points at one precise DN.  The vPC pair is an
**atomic class** — in staged mode `fabricExplicitGEp` ships with both node
endpoints in a single request, because the APIC validates the pair as a
unit ({doc}`../guide/push-modes`).

## Verify

```python
pool = aci.query("fvnsVlanInstP").fetch()
assert pool[0].name == "prod-static"

pairs = aci.query("fabricExplicitGEp").fetch()
assert [p.name for p in pairs] == ["101-102"]

assert config.push(aci, mode="plan").has_changes is False
```

## Variations & pitfalls

- **Single-homed servers** — same chain with
  `policies.access_group("single-server").bind(aaep=..., cdp=..., lldp=...)`
  and the selector bound to that group instead.
- **Naming is your API** — profiles named after racks (`rack12-…`) and
  policies named after behaviour (`cdp-on`) keep day-2 designs readable;
  ACI will not rename anything for you later.
- **The pair must exist before static vPC paths** — a `protpaths-101-102`
  static path (next recipe) resolves against this protection group; push
  the rack before the workloads.
- **Reusing the pool** — a second rack reuses `prod-aaep` and friends by
  name: declare only the new profiles and `bind_dn()` the existing AAEP, or
  redeclare the shared chain — upserts make redeclaration harmless.
