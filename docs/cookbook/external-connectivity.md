# Give the tenant a way out — an L3Out

**Problem** — the storefront has to reach the internet-facing edge, and return
traffic has to find its way back into the `prod` VRF.  That is an **L3Out**: a
border leaf with a router ID, a routed link to the WAN router, a routing
protocol on that link, and an external EPG that classifies the outside world and
decides what it may reach.  The whole chain is curated vocabulary — typed,
checked closed-world, IDE-completed.

Same `commerce` deployment; this adds its edge.

## The design

The encap on the routed link comes from a VLAN pool tied to an L3 domain — the
routing side and the access side meet there.  OSPF runs on a routed
sub-interface toward the WAN router; a tenant-level OSPF interface policy sets
the network type:

```python
from niwaki import Niwaki
from niwaki.design import tenant

config = tenant("commerce")
config.vrf("prod")

# Encap plumbing for the routed link
config.infra().vlan_pool("commerce-l3", "static").range("vlan-3010", "vlan-3010")
config.l3_dom("commerce-l3dom").bind(vlan_pool="commerce-l3")

# Interface-level protocol policy, referenced by name below
config.ospf_interface_policy("ospf-p2p", network_type="p2p")

l3out = config.l3out("edge").bind(vrf="prod").bind(domain="commerce-l3dom")
l3out.ospf(area_id="0.0.0.10", area_type="regular")

# Border leaf 101 with its router ID
nodes = l3out.node_profile("border-leaves")
nodes.node_attachment("topology/pod-1/node-101", rtr_id="10.30.255.101")

# Routed sub-interface toward the WAN router, OSPF on the link
links = nodes.interface_profile("uplinks")
links.path_attachment(
    "topology/pod-1/paths-101/pathep-[eth1/33]",
    if_inst_t="sub-interface",
    addr="192.0.2.2/30",
    encap="vlan-3010",
)
links.ospf_interface().bind(ospf_interface_policy="ospf-p2p")

# The security half: what the outside is, and what it may consume
config.filter("f-https").entry("https", tcp=443)
config.contract("inbound-web").set(scope="vrf").subject("web").bind(filter="f-https")

external = l3out.external_epg("default-route")
external.subnet("0.0.0.0/0")
external.provide("inbound-web")
```

Everything resolves inside the design.  `bind(domain="commerce-l3dom")` finds
the declared L3 domain (the alias accepts L2 or L3 domains — the target is
abstract), and `bind(ospf_interface_policy="ospf-p2p")` finds the tenant policy.
The two `topology/...` arguments are the only literal DNs: node and path
attachments name physical topology, which lives outside `uni`.  Each bind sits
on the object that owns it — the L3Out binds its VRF and domain, the OSPF
interface binds its policy.

## Plan, push

```python
aci = Niwaki.connect("https://apic.example.com", "admin", "secret")

plan = config.push(aci, mode="plan")
print(f"{len(plan.creates)} objects")
config.push(aci)
```

The `with` form closes the session for you; `connect()` is used here so the
rest of the page can share one client — see {doc}`../guide/connection`.

## Verify

```python
outs = aci.query("l3extOut").fetch()
assert [o.name for o in outs] == ["edge"]

attached = aci.query("l3extRsNodeL3OutAtt").fetch()
assert [n.rtr_id for n in attached] == ["10.30.255.101"]

assert config.push(aci, mode="plan").has_changes is False
```

## BGP instead of OSPF

The BGP flavor swaps the protocol block — peers hang off the node profile
(loopback peering) or the interface profile (interface peering):

```python
bgp_out = tenant("commerce").l3out("edge-bgp")
bgp_out.bgp()
peer = bgp_out.node_profile("border-leaves").bgp_peer("192.0.2.1")
peer.autonomous_system_profile(autonomous_system_number="65010")
```

`bgp_peer(...)` takes the neighbor address; the remote AS is its
`autonomous_system_profile` child — all curated.

## Variations & pitfalls

- **The encap must be in the pool** — `vlan-3010` resolves only because
  `commerce-l3` covers it; the APIC rejects the interface otherwise, at push
  time.
- **Advertising BD subnets** — a BD whose subnet must leave the fabric needs
  `bd.bind(l3out="edge")` **and** the subnet scoped `public`; keep both in the
  same design so the intent reviews as one change.
- **The default-route external EPG** — `0.0.0.0/0` with the default
  `import-security` scope classifies *all* outside traffic into one external
  EPG; split it into specific prefixes when different externals deserve
  different contracts.
- **Router IDs are per node** — one loopback-derived router ID per border leaf;
  keep them inside the fabric's management range and unique.
