# 06 — External connectivity (L3Out / L2Out)

Exhaustive, non-production walkthroughs that provision external connectivity on a
live APIC through the niwaki SDK and let the controller accept or reject every
object. The goal is to prove the SDK can *express* each L3Out / L2Out capability
and every valid combination of it — not to model a real network. All values
(subnets, ASNs, router-ids, VLANs) are illustrative.

```bash
uv run pytest tests/integration/06_l3out -m integration -s
```

Everything lands in one tenant, `niwaki-it-l3out` (plus a handful of named objects
in `infra` for the SR-MPLS handoff). Encaps draw from a single VLAN lane,
`vlan-2600`–`vlan-2699`. Router-ids and interface addresses use a `10.x` scheme.
Each file owns a manual `wipe()` (run via `tests/integration/wipe.py`, never by the
suite).

## Stats

| | |
|---|---|
| Files | 13 (`test_001` … `test_013`) |
| Test functions | 21 |
| Live result | **20 pushed green, 1 documented skip** |
| Config objects provisioned | **≈900 managed objects** (counted across ~50 sampled classes; the full child tree is larger) |
| L3Outs / L2Outs | 33 / 3 |
| VRFs | ~35 (one per L3Out where router-id or loopback uniqueness demands it) |
| BGP peers / static routes / ext-EPG subnets | 22 / 20 / 15 |
| Route-maps (`rtctrlProfile` variants) | 13 |
| Route-map bindings | 17 managed objects across 9 attachment-point × direction combinations |
| Config rejections | **0** — the APIC accepts every object the suite pushes |
| Config faults | **0** — every accepted object is also config-clean (the floating-SVI floating address is set in-subnet via `ref(domain, floating_addr=…)`, so no `subnet-mismatch`). |
| Residual faults | runtime/deployment only (BGP peers and interfaces with no real external neighbour on the simulator; L2Out static paths without a wire encap). These would clear on hardware with real peers. |

Parent objects carry a one-phrase `description=` of the coverage they provide (the
tenant, each L3Out / L2Out, and each route-map), so the tree reads as
self-documenting in the GUI.

## What the suite covers

Files are organised by object, and each sweeps that object's combination axes —
one managed object per representative combination, factored across multiple L3Outs
/ VRFs where one object cannot hold two mutually-exclusive settings at once.

- **`test_001_l3out_base`** — L3Out roots over the `enforce_rtctrl` flag set × MPLS
  on/off × router-id-loopback on/off; the default-route leak policy across its
  `always` × `criteria` × `scope` matrix; both route-target instrumentation modes;
  consumer labels (both ownerships).
- **`test_002_interfaces_routed`** — routed (`l3-port`) interfaces over the
  MTU (numeric + `inherit`) × IPv6-DAD × target-DSCP matrix, each with a secondary
  address.
- **`test_003_interfaces_svi`** — SVIs over the tag-mode × encap-scope × autostate
  matrix (each with a secondary address and a rogue-exception-MAC); sub-interfaces
  over encap-scope × MTU; floating SVIs with per-side member nodes, secondary
  addresses and an ND prefix profile.
- **`test_004_bgp`** — node-level BGP peers: one per local-AS propagation mode, one
  per neighbour max-prefix action (each with its own prefix policy + route-control),
  one per private-AS control, a fully-loaded peer exercising the address-family /
  peer / BFD control flags at once, and a peer with a site-of-origin; best-path +
  timers protocol profile.
- **`test_005_ospf`** — one L3Out per OSPF area type (regular / stub / NSSA) with a
  representative area-control set, authenticated OSPF interfaces (none / simple /
  md5) and both interface-policy network types.
- **`test_006_eigrp`** — EIGRP autonomous system with authenticated interfaces (key
  chain), one interface profile per interface-control flag; VRF EIGRP
  address-family context (both metric styles).
- **`test_007_static_routes`** — static routes over the route-control flag ×
  aggregated (with prefix-length window) × administrative-preference matrix, with
  forwarding next hops.
- **`test_008_route_control`** — every match term (prefix lists with length
  windows, community terms + factors over both scopes, community + AS-path regex)
  and every `set` clause across its enums, assembled into combinable and global
  route-maps with permit/deny contexts.
- **`test_009_external_epgs`** — external EPGs over the preferred-group ×
  enforcement × QoS matrix; the `l3extSubnet` scope flags across a broad set of
  valid combinations; each route-aggregation flavour on its own default route; the
  full contract-label set and the provide / consume / intra-EPG verbs.
- **`test_010_l2out`** — one L2Out per external-EPG attribute mix (an l2extOut
  allows a single external EPG), each with a node/interface profile + static path,
  the `fvSubnet` scope × data-plane-learning matrix as `/32` host routes, contract
  labels and provide/consume verbs.
- **`test_011_srmpls`** — the SR-MPLS handoff: the tenant side (MPLS-enabled L3Out
  + consumer label) pushes live; the infra side (label reference, node SID, infra
  peer + data-plane, MPLS interface, provider label, MPLS custom-QoS) is compiled
  and resolved by the SDK but its live push is a documented skip (see below).
- **`test_012_protocol_interface_matrix`** — the routing-protocol × interface-type
  cross-product, factored one L3Out per cell: OSPF and EIGRP each over routed,
  sub-interface and SVI interfaces.
- **`test_013_route_map_bindings`** — route-maps bound at **every attachment point,
  in both directions**: BGP peer import + export (node peer and loopback peer);
  external-EPG `l3extInstP` import + export; external-EPG subnet import + export;
  the L3Out `default-import` and `default-export` profiles; the redistribution
  route-map per source (static / direct / attached-host); interleak and dampening.

## APIC combination constraints discovered live

The schema validates a field in isolation; the controller enforces many
cross-field and cross-object rules the schema does not. These were found by
pushing and reading the rejection, then encoded (or factored around) so every
object the suite pushes is accepted.

**Factored onto separate objects (both sides covered):**

- **OSPF and EIGRP are mutually exclusive on one L3Out** → each rides its own
  L3Out (`test_005`, `test_006`, `test_012`).
- **A protocol interface (`ospfIfP` / `eigrpIfP`) is a singleton per interface
  profile** → each authentication type / control flag rides its own interface
  profile or L3Out.
- **Consumer and provider labels cannot share an L3Out** → separate L3Outs; the
  provider label is additionally **infra-tenant only**, so it rides the SR-MPLS
  infra L3Out.
- **Route summarization (BGP / OSPF / EIGRP) resolves to one relation class** on a
  subnet → each flavour rides its own subnet.
- **A route-aggregation flavour applies to one default route**, and an EPG holds a
  single `0.0.0.0/0` → one external EPG per aggregate flavour.
- **Regex and non-regex community match terms cannot share a match rule** → the
  regex terms live in their own rule.
- **`set_next_hop_unchanged` conflicts with `set_route_tag` / an explicit next
  hop, and `set_redistribute_multipath` requires next-hop propagation** → those
  clauses form their own action rule.
- **Import and export route-maps are distinct relations** → each attachment point
  binds a separate route-map per direction (`test_013`).
- **The routing-protocol × interface-type cross-product** cannot vary on one L3Out
  → one L3Out per cell (`test_012`).

**Encoded as value rules:**

- `enforce_rtctrl` — export route control is always enforced; import-only is
  rejected.
- A loopback IP is unique to one L3Out within a VRF, and a node's router-id is
  fixed per VRF → most L3Outs take their own VRF.
- `l3extSubnet` scope — a `shared-security` subnet must also carry
  `import-security`.
- BGP graceful-restart controls accept only `helper`; private-AS control needs all
  three flags (`remove-all`, `remove-exclusive`, `replace-as`) together.
- `add_community` accepts only the `append` criterion; `set_community` `none`
  carries no community; `set_as_path` `last_as_number` must be `0` for plain
  prepend; a route-control context `local_order` ≤ 9.
- The interleak / redistribution route-maps must be permit-only, and the
  attached-host redistribution map cannot carry a metric set rule.
- Sub-interfaces are always tagged (`regular` only); OSPF auth keys ≤ 8 chars;
  site-of-origin takes the extended-community form.
- L2Out — exactly one external EPG; subnets are `/32` host routes with
  `no-default-gateway`; `intra_epg` is not supported (provide/consume only).
- SR-MPLS — the MPLS label policy is the singleton `default` under tenant infra
  and cannot be modified; MPLS custom-QoS and its node-profile binding are
  infra / overlay-1 only; an MPLS infra L3Out uses border leaves, not spines; the
  node SID lives on the transport loopback; the egress EXP rule takes no target
  DSCP.

## Skipped / uncoverable, and why

Genuinely uncoverable on this fabric (hardware / underlay / topology dependent, or
a curated maker with a precondition the L3Out domain cannot meet). Each is reported
in a `# COVERAGE GAP` note in its file:

- **SR-MPLS infra handoff (`test_011`)** — compiled and resolved by the SDK, live
  push skipped. This two-leaf fabric's border leaves are already claimed by an
  existing SR-MPLS infra L3Out, and `overlay-1` enforces one BGP-EVPN and one
  MPLS-transport loopback per node while an L3Out may not share a loopback with
  another — the two rules cannot both hold for a second infra L3Out on the same
  leaves. It needs dedicated border leaves and a real SR-MPLS underlay.
- **`ptpRtdEpgCfg` (PTP on an interface)** — needs a fabric PTP profile;
  `unicast-slave` mode is additionally rejected on an L3Out interface.
- **`bfdMicroBfdP` (micro-BFD)** — the APIC requires it on a routed *port-channel*
  interface, which needs an access bundle policy group the L3Out domain does not
  provide.
- **Static-route discard next hop (`nexthop_type="none"`)** — rejected with any
  address, and the maker requires the address positionally.
- **`l3extBdProfileCont` on a floating SVI** — accepted only with a *physical*
  domain, not the L3 domain used here.
- **`mplsSrgbLabelPol` (SRGB)** — would modify the singleton default label policy,
  which is not supported.
- **`l3extInfraNodeP` (infra node / spine role)** — a GOLF / multipod construct,
  rejected on an MPLS L3Out.
- **Per-path BGP peers (`bgpPeerP` / `bgpInfraPeerP` on `l3extRsPathL3OutAtt`)** —
  not surfaced on the path-attachment cursor; node-level peers are covered.
- **IP-SLA route tracking (`ipRsRouteTrack`, `ipRsNexthopRouteTrack`,
  `ipRsNHTrackMember`)** — not surfaced on the static-route / next-hop cursor.
- **NAT mapping EPG (`l3extRsInstPToNatMappingEPg`); remote-site / orchestrator
  makers (`fvSiteAssociated`, `mdpClassId`, `fvOrchsInfo`, `vns*`)** — cloud / NDO
  / service-graph scope, covered in their own phases.
- **`contract_master` on an L2Out external EPG, `fvCEp` static endpoint** — an
  l2extOut allows only one external EPG (no sibling to inherit from), and NLB /
  anycast / endpoint children are app-EPG-only.

See [`../README.md`](../README.md) for what these walkthroughs are — and are not.
