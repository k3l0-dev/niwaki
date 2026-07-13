# A complete L3Out

**Problem** — the `prod` VRF needs a routed way out of the fabric: a border
leaf with a router ID, a routed sub-interface toward the WAN router, OSPF on
the link, and an external EPG that governs what the outside may reach.
The whole chain is curated vocabulary — every level below is typed, checked
closed-world, and IDE-completed.

## The design

```python
from niwaki import Niwaki
from niwaki.design import tenant

config = tenant("shop")
config.vrf("prod")
config.l3_dom("wan-dom")

# Tenant-level protocol policy, referenced by name further down
config.ospf_interface_policy("ospf-p2p", network_type="p2p")

l3out = config.l3out("wan").bind(vrf="prod", domain="wan-dom")
l3out.ospf()                                    # enable OSPF on the L3Out

# Border leaf: node 101 with its router ID
nodes = l3out.node_profile("border-leaves")
nodes.node_attachment("topology/pod-1/node-101", rtr_id="10.0.0.101")

# Routed sub-interface toward the WAN router, OSPF on the link
interfaces = nodes.interface_profile("uplinks")
interfaces.path_attachment(
    "topology/pod-1/paths-101/pathep-[eth1/33]",
    if_inst_t="sub-interface",
    addr="192.0.2.2/30",
    encap="vlan-3900",
)
interfaces.ospf_interface().bind(ospf_interface_policy="ospf-p2p")

# The security half: what the outside is, and what it may consume
config.filter("any-ip").entry("ip", ethernet_type="ip")
config.contract("outbound").set(scope="vrf").subject("all").bind(filter="any-ip")

external = l3out.external_epg("internet")
external.subnet("0.0.0.0/0")
external.consume("outbound")
```

Everything resolves inside the design: `bind(domain="wan-dom")` finds the
declared L3 domain (the alias accepts L2/L3 domains — the target is
abstract), `bind(ospf_interface_policy=...)` finds the tenant-level policy,
and a typo in any of them fails **before any request** with a did-you-mean.
The two `topology/...` arguments are the only literal DNs — node and path
attachments name physical topology, which lives outside `uni`
({doc}`static-paths` explains the DN shapes).

## Plan, push, verify

```python
aci = Niwaki.connect("https://apic.example.com", "admin", "secret")

plan = config.push(aci, mode="plan")
print(f"{len(plan.creates)} objects")
config.push(aci)

outs = aci.query("l3extOut").fetch()
assert [o.name for o in outs] == ["wan"]

peers = aci.query("l3extRsNodeL3OutAtt").fetch()
assert [p.rtr_id for p in peers] == ["10.0.0.101"]

assert config.push(aci, mode="plan").has_changes is False
```

The `with` form closes the session for you; `connect()` is used here so the
rest of the page can share one client — see {doc}`../guide/connection`.

## BGP instead of OSPF

The BGP flavor swaps the protocol block — peers hang off the node profile
(loopback peering) or the interface profile (interface peering):

```python
bgp_out = tenant("shop").l3out("wan-bgp")
bgp_out.bgp()
peer = bgp_out.node_profile("border-leaves").bgp_peer("192.0.2.1")
peer.autonomous_system_profile(autonomous_system_number="65002")
```

`bgp_peer(...)` takes the peer address; the remote AS is its
`autonomous_system_profile` child, and prefix limits bind to a tenant-level
`bgp_peer_prefix_policy` — all curated.

## Variations & pitfalls

- **SVI / floating SVI** — `path_attachment(..., if_inst_t="ext-svi")` for a
  classic SVI; `interfaces.floating_svi(anchor_node_dn, encap, ...)` for
  anchor-based floating SVIs (VMM-mobile routers).
- **The encap must be in the domain's pool** — `wan-dom` needs a
  `bind(vlan_pool=...)` covering `vlan-3900`, exactly as in
  {doc}`access-policies-vpc`.
- **`0.0.0.0/0` scope** — an external subnet classifies traffic
  (import-security) by default; advertising and aggregation are flags on the
  subnet, and route summarization binds to per-protocol tenant policies
  (`bgp_route_summarization_policy`, …).
- **Advertising BD subnets** — a BD whose subnet must be advertised needs
  `bd.bind(l3out="wan")` *and* the subnet scoped `public`; keep both in the
  same design so the intent reviews as one change.
- **Static routes** — hang off the node attachment; declare them with
  `nodes.node_attachment(...)` children via `.mo()` ({doc}`../guide/models`).
