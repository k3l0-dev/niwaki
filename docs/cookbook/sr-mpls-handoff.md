# SR-MPLS handoff to a backbone

**Problem** — the `commerce` VRF must hand its routes to an SR-MPLS backbone (a
DC interconnect, or a provider core) using a segment-routing MPLS handoff.  This
is the most involved external-connectivity task in ACI, and the reason is
structural: **the configuration spans two administrative places that never
touch the same object.**  Getting that split right is the whole recipe.

- The **infra side** builds the transport: the SR-MPLS underlay on the fabric's
  border leaves, under tenant `infra` / VRF `overlay-1`.  It owns the label
  policy, the BGP-EVPN session to the backbone, the node SIDs, the MPLS
  interfaces, the MPLS EXP marking, and — critically — the **provider label**.
- The **tenant side** builds the handoff for one VRF: an MPLS-enabled L3Out
  under the user tenant, plus a **consumer label** that stitches onto the infra
  provider label by name.

The tenant operator never configures transport; the fabric operator never
configures tenant routes.  The label pair is the seam between them.

## Why each object lives where it does

| Object                               | Side       | Why                                                          |
| ------------------------------------ | ---------- | ------------------------------------------------------------ |
| MPLS global label policy (`default`) | infra      | A fabric singleton — the SRGB is fabric-wide, not per-tenant |
| MPLS interface policy                | infra      | Describes the physical handoff link                          |
| BGP-EVPN infra peer + data-plane     | infra      | The session to the backbone lives in `overlay-1`             |
| Node SID on the transport loopback   | infra      | Segment routing identity is a fabric-node property           |
| MPLS interface                       | infra      | The handoff port is fabric transport                         |
| **Provider label**                   | infra      | Provider labels are permitted only on an infra-tenant L3Out  |
| MPLS custom-QoS (EXP marking)        | infra      | The node-profile QoS bind is supported only on `overlay-1`   |
| MPLS-enabled L3Out                   | **tenant** | The routes being handed off belong to the tenant VRF         |
| **Consumer label**                   | **tenant** | It names the provider label to stitch onto                   |

## The infra side — build the underlay first

The transport must exist before a tenant can consume it.  Everything here is
under tenant `infra`, and the whole design compiles and resolves closed-world:

```python
from niwaki import Niwaki
from niwaki.design import tenant

# Border leaves that carry the handoff — an SR-MPLS infra L3Out rides border
# *leaves* (the APIC rejects spine nodes on an MPLS L3Out).
border_leaves = [("border-101", 101), ("border-102", 102)]

inf = tenant("infra")

# The MPLS global label policy is the fabric singleton "default".  It cannot be
# modified — it is referenced attribute-free, purely to resolve the binding.
inf.mpls_global_configuration("default")
inf.mpls_interface_policy("mpls-backbone", description="MPLS handoff interface policy.")
inf.bgp_peer_prefix_policy("sr-mpls-limit", max_number_of_prefixes=20000, max_prefix_action="log")

# MPLS EXP marking is supported only under tenant infra, so it lives here and
# the node profile binds it by name below.
qos = inf.mpls_custom_qos_policy("mpls-exp-marking", description="MPLS EXP marking for the handoff.")
qos.mpls_ingress_rule("0", "3", prio="level3", target="CS3", target_cos="3", description="EXP in.")
qos.mpls_egress_rule("0", "31", target_cos="5", target_exp="5", description="DSCP out.")

# Encap plumbing for the handoff links
inf.infra().vlan_pool("sr-mpls-underlay", "static").range(
    "vlan-2690", "vlan-2699", allocation_mode="static", role="external"
)
inf.l3_dom("sr-mpls-idom").bind(vlan_pool="sr-mpls-underlay")

# Reference the fabric infra VRF (overlay-1) — attribute-free, so it is only made
# resolvable, never reconfigured.
inf.vrf("overlay-1")

# The infra SR-MPLS L3Out, its MPLS-external config, and the provider label
out = inf.l3out("sr-mpls-infra", mpls_enabled=True).bind(vrf="overlay-1").bind(domain="sr-mpls-idom")
out.mpls_external(description="MPLS handoff config.").bind(mpls_global_configuration="default")
out.provider_label("sr-backbone", tag="green", description="SR-MPLS provider label.")
```

Now the per-node handoff.  Each border leaf gets a node profile (binding the
MPLS custom-QoS), a transport loopback carrying the node SID, a BGP-EVPN peer to
the backbone, and an MPLS-enabled interface:

```python
for idx, (name, node_id) in enumerate(border_leaves, start=1):
    np = out.node_profile(f"np-{name}", description=f"SR-MPLS node profile for {name}.").bind(
        mpls_custom_qos_policy="mpls-exp-marking"
    )

    # The router-id doubles as the BGP-EVPN loopback; a separate transport
    # loopback carries the node SID.
    evpn = f"10.10.10.{node_id}"
    transport = f"20.20.20.{node_id}"
    att = np.node_attachment(f"topology/pod-1/node-{node_id}", rtr_id=evpn, rtr_id_loop_back=True)
    loop = att.loopback(transport, description="SR-MPLS transport loopback.")
    loop.node_sid(srgb_index=1, loopback_addr=transport, description="Node SID.")

    # The BGP-EVPN session to the backbone: remote AS, local AS, data-plane loopback.
    peer = np.infra_peer_connectivity_profile(
        f"10.11.2.{idx}",
        peer_type="sr-mpls",
        administrative_state="enabled",
        ebgp_multihop_ttl_value=2,
        password="sr-mpls-secret",
        description="SR-MPLS EVPN peer.",
    )
    peer.autonomous_system_profile(autonomous_system_number=65000, description="Remote AS.")
    peer.local_autonomous_system_profile(local_asn=65100, asn_propagation="none")
    peer.data_plane(mdp_data_plane_address=f"10.11.3.{idx}", description="MPLS data-plane loopback.")
    peer.bind(bgp_peer_prefix_policy="sr-mpls-limit")

    # The MPLS-enabled handoff interface
    ifp = np.interface_profile(f"if-{name}")
    ifp.path_attachment(
        f"topology/pod-1/paths-{node_id}/pathep-[eth1/60]",
        if_inst_t="sub-interface",
        addr=f"10.11.4.{idx}/30",
        encap="vlan-2690",
    )
    ifp.mpls_interface(description="MPLS-enabled interface.").bind(mpls_interface_policy="mpls-backbone")
```

Note every bind sits on the object that owns it: the node profile binds its
custom-QoS, the MPLS-external binds the global label policy, the peer binds its
prefix policy, the MPLS interface binds its interface policy.

## The tenant side — hand off one VRF

The tenant L3Out carries neither the label policy nor the node-profile QoS —
both live only on `overlay-1`.  It references the infra handoff through the
**consumer label**, whose name must match the provider label declared above:

```python
t = tenant("commerce")
t.vrf("prod")
t.infra().vlan_pool("sr-mpls-handoff", "static").range(
    "vlan-2600", "vlan-2699", allocation_mode="static", role="external"
)
t.l3_dom("sr-mpls-tdom").bind(vlan_pool="sr-mpls-handoff")

edge = t.l3out("sr-mpls-edge", mpls_enabled=True).bind(vrf="prod").bind(domain="sr-mpls-tdom")

for idx, (name, node_id) in enumerate(border_leaves, start=1):
    np = edge.node_profile(f"np-{name}")
    np.node_attachment(f"topology/pod-1/node-{node_id}", rtr_id=f"10.12.0.{idx}", rtr_id_loop_back=False)

# The consumer label stitches onto the provider label of the same name.
edge.consumer_label(
    "sr-backbone",
    represents_the_provider_label_ownership="infra",
    description="Consume the infra SR-MPLS handoff.",
)
```

## Describe, plan, push

The infra design is dense — inspect the compiled envelope offline before it goes
anywhere:

```python
payload = inf.to_payload()
assert payload["polUni"]["children"]     # the whole infra handoff, resolved
```

Then plan and push each side.  The underlay first, so the provider label exists
when the tenant consumes it:

```python
aci = Niwaki.connect("https://apic.example.com", "admin", "secret")

infra_plan = inf.push(aci, mode="plan")
print(f"infra: {len(infra_plan.creates)} objects")
inf.push(aci)

tenant_plan = t.push(aci, mode="plan")
print(f"tenant: {len(tenant_plan.creates)} objects")
t.push(aci)
```

The `with` form closes the session for you; `connect()` is used here so the
rest of the page can share one client — see {doc}`../guide/connection`.

## Verify

The seam is the label pair — confirm both ends landed and name-match:

```python
provider = aci.query("l3extProvLbl").fetch()
consumer = aci.query("l3extConsLbl").fetch()
assert [p.name for p in provider] == ["sr-backbone"]
assert [c.name for c in consumer] == ["sr-backbone"]

outs = {o.name for o in aci.query("l3extOut").fetch()}
assert {"sr-mpls-infra", "sr-mpls-edge"} <= outs

assert t.push(aci, mode="plan").has_changes is False
```

## Variations & pitfalls

- **Never modify the default label policy** — `mpls_global_configuration` is the
  fabric singleton `default`; reference it, but do **not** call `.srgb()` on it.
  The APIC rejects it with *"MPLS Default Label Policy Modification is not
  supported"*.  A custom SRGB is a fabric-wide decision made elsewhere.
- **Border leaves, not spines** — an SR-MPLS infra L3Out rides border *leaves*.
  The spine-role infra node construct (`l3extInfraNodeP`, a GOLF/multipod
  object) is rejected on an MPLS L3Out.
- **Provider is infra-only** — a provider label on a user-tenant L3Out is
  refused; only the infra-tenant L3Out may provide.  The tenant side always
  *consumes*.
- **Names stitch the halves** — the consumer label's name (`sr-backbone`) must
  equal the provider label's name; that string is the entire link between the
  two designs.  A mismatch is a silent no-handoff, not an error.
- **Topology-bound** — `overlay-1` fixes one BGP-EVPN and one MPLS-transport
  loopback per node, and an L3Out may not share a loopback with another, so a
  second infra L3Out needs its own dedicated border leaves.  Plan the border
  leaf assignment before you build.
- **Custom-QoS is a node-profile bind** — MPLS EXP marking attaches to the infra
  node profile, not the interface; declare the `mpls_custom_qos_policy` under
  tenant infra and bind it there ({doc}`../guide/design-dsl`).
