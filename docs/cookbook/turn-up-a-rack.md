# Turn up a new rack

**Problem** — rack B7 is racked and cabled: ESXi hosts dual-homed to leaves 101
and 102 over a vPC.  Before a single VM can land, the fabric needs the whole
access-policy chain — interface policies, a VLAN pool, a physical domain, an
AAEP, a vPC policy group, interface and switch profiles — plus the vPC
protection pair on the fabric side.  It is the canonical "twelve GUI screens"
task; here it is one design.  Then one static path connects the `web` EPG to the
wire.

Same `commerce` deployment; this is the physical bring-up beneath it.

## The design

`design()` opens an empty multi-domain design, so `infra`, `fabric` and the
tenant all live in one push:

```python
from niwaki import Niwaki
from niwaki.design import design

config = design()
inf = config.infra()

# Interface policies — reusable, so named after behaviour, not location
inf.cdp_policy("cdp-on", admin_state="enabled")
inf.lldp_policy("lldp-on", receive_state="enabled", transmit_state="enabled")
inf.lacp_policy("lacp-active", mode="active")

# Encap: pool -> physical domain -> AAEP
inf.vlan_pool("commerce-static", "static").range("vlan-1410", "vlan-1449")
config.phys_dom("commerce-phys").bind(vlan_pool="commerce-static")
inf.aaep("commerce-aaep").bind(domain="commerce-phys")

# The vPC policy group ties the interface policies and the AAEP together
groups = inf.func_profile()
groups.port_channel("esxi-vpc", link_aggregation_type="node").bind(
    aaep="commerce-aaep", cdp="cdp-on", lldp="lldp-on", lacp="lacp-active"
)

# Interface profile: which ports...
ports = inf.access_port_profile("rack-b7-ports")
uplinks = ports.port_selector("esxi-uplinks", "range")
uplinks.port_block("blk1", from_port_id=10, to_port_id=11)
uplinks.bind(policy_group="esxi-vpc")

# ...on which switches
leaves = inf.leaf_profile("rack-b7-leaves")
leaves.leaf_selector("pair", "range").node_block("blk1", from_node_id=101, to_node_id=102)
leaves.bind(interface_profile="rack-b7-ports")

# Fabric side: the explicit vPC protection pair
pair = config.fabric().vpc_protection().vpc_pair("101-102", logical_pair_id="7")
pair.node("101")
pair.node("102")
```

Each bind lands on the object that owns it: the physical domain binds its pool,
the AAEP binds its domain, the policy group binds its interface policies, and the
port selector binds its policy group.  `policy_group` targets an *abstract*
class, so the same alias would accept an `access_group` for single-homed servers.

## Connect the EPG to the wire

The access policies exist; now the `web` EPG joins the physical domain and lands
on the vPC.  A static path is the one relation whose target lives *outside*
`uni` — the path DN names physical topology — so it is a maker with a literal
DN, not a bind:

```python
tn = config.tenant("commerce")
tn.vrf("prod")
tn.bd("bd-web", unicast_routing=True).bind(vrf="prod").subnet("10.30.10.1/24")

web = tn.app("storefront").epg("web").bind(bd="bd-web").bind(domain="commerce-phys")
web.static_path(
    "topology/pod-1/protpaths-101-102/pathep-[esxi-vpc]",
    encap="vlan-1410",
    deployment_immediacy="immediate",
)
```

The `protpaths-101-102` segment is the vPC pair; `pathep-[esxi-vpc]` names the
policy group, not a physical port.

## Plan, push

Access policies on a brownfield fabric are a good fit for `staged` mode: each
object lands in its own request, so a conflict points at one precise DN:

```python
aci = Niwaki.connect("https://apic.example.com", "admin", "secret")

plan = config.push(aci, mode="plan")
print(f"{len(plan.creates)} objects to create")

report = config.push(aci, mode="staged")
```

The `with` form closes the session for you; `connect()` is used here so the
rest of the page can share one client — see {doc}`../guide/connection`.

The vPC pair is an **atomic class**: in staged mode `fabricExplicitGEp` ships
with both node endpoints in a single request, because the APIC validates the
pair as a unit ({doc}`../guide/push-modes`).

## Verify

```python
pool = aci.query("fvnsVlanInstP").fetch()
assert pool[0].name == "commerce-static"

pairs = aci.query("fabricExplicitGEp").fetch()
assert [p.name for p in pairs] == ["101-102"]

paths = aci.query("fvRsPathAtt").fetch()
assert [p.encap for p in paths] == ["vlan-1410"]

assert config.push(aci, mode="plan").has_changes is False
```

## Variations & pitfalls

- **Single-homed servers** — same chain with
  `groups.access_group("bare-metal").bind(aaep=..., cdp=..., lldp=...)` and the
  selector bound to that group; a bare access port uses `paths-101` in the
  static-path DN, not `protpaths-101-102`.
- **The pair before the path** — a `protpaths-101-102` static path resolves
  against the protection group and the `esxi-vpc` policy group, so both must
  exist first.  One `design()` guarantees the ordering; the push engine lands
  parents before children.
- **Naming is your API** — profiles named after the rack (`rack-b7-…`) and
  policies named after behaviour (`cdp-on`) keep day-2 designs readable; ACI
  will not rename anything for you later.
- **`mode`** on the path — `regular` (the default) trunks the VLAN tagged;
  `native` sends it untagged; hypervisor uplinks almost always want tagged.
- **Reusing the pool** — a second rack reuses `commerce-aaep` and friends by
  name: declare only the new profiles, and either redeclare the shared chain
  (upserts make it harmless) or `bind_dn()` the existing AAEP by DN.
