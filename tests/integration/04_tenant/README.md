# 04 — Tenant networking

Tenant core provisioning, driven to **combination coverage**: VRFs, bridge
domains, application profiles / EPGs, and endpoint security groups, together with
the policies and children that hang off them. The suite pushes each object with
its attributes swept across the SDK's surface — every enum value at least once,
every boolean both ways, `Flags` fields empty / one / several, and a cartesian
over the fields that interact — so it proves the SDK can express the whole tenant
config surface and that the controller accepts what it produces.

```bash
uv run pytest tests/integration/04_tenant -m integration -s
```

These are **not** production configurations. The values (subnets, MACs, VLANs,
policy settings) are illustrative — chosen to exercise the SDK, not to model a
real tenant. See [`../README.md`](../README.md) for what these walkthroughs are.

## Stats

| Metric | Value |
| --- | --- |
| Files | 14 (`test_001`..`test_014`) |
| Test functions | 30 |
| Config objects pushed (MOs) | 4271 |
| Dedicated tenants | 14 |
| Live result | 30/30 passed |
| Critical / major faults | 0 |

Every object is accepted by the controller with **zero critical and zero major
faults**. A few sweeps are deliberately partial — static paths carry no access
domain (domains live in the fabric-access phase) and route-summarization has no
matching node/subnet — so they raise the expected **benign minor/warning faults**
(F0467, F0524, F4308). Those are a property of the illustrative, isolated config,
not a rejection or a config error.

The matrix is **factored across dedicated tenants** — one per slice
(`niwaki-it-vrf`, `niwaki-it-bd-proxy`, `niwaki-it-bd-flood`, `niwaki-it-bd-l2`,
`niwaki-it-epg`, `niwaki-it-sp-a`, `niwaki-it-esg`, …), and large sweeps are
further split across several VRFs and test functions so each push stays a few
hundred objects. Each file owns the tenant it creates and a `wipe(aci)` that
deletes it (a cascading delete); the runner `tests/integration/wipe.py 04_tenant`
is operator-only and is never called by the suite. Cartesians are generated with
`itertools.product`, so mutually exclusive combinations simply become separate
objects.

The largest multipliers: **360 bridge domains** across the full valid forwarding
cartesian (unknown-unicast x ARP x unicast-routing x multi-destination x unknown
v4/v6 multicast x move-detection x IPv6-multicast-allow x limit-IP-learn), each
routed BD carrying four subnets — **1280 subnets**; a **static-path binding on
every one of a leaf's ~60 front-panel interfaces** (data-driven from `l1PhysIf`
at runtime, for both leaves); **112 EPGs** across six application profiles;
**112 ESGs** across four application profiles; and the **full 55-cell
VM-attribute matrix** on one uSeg criterion.

## What the suite covers

Per major object, the axes swept (and cartesian'd where fields interact):

- **VRF (`fvCtx`)** — cartesian of enforcement preference (enforced / unenforced)
  x enforcement direction (ingress / egress), spread across data-plane learning
  (enabled / disabled), known-multicast action (permit / deny) and BD
  enforcement (both). Every protocol-policy bind (BGP timers, BGP / EIGRP
  address-family, OSPF timers, endpoint retention, route-tag, VRF validation,
  monitoring), each bind target built in variants covering its own enums
  (BGP graceful-restart on/off, EIGRP narrow/wide metrics, OSPF control knobs).
  Simple children: DNS labels, SNMP context, global name, route summarization,
  route deployment (automatic / contract), BGP route targets (IPv4 / IPv6).
- **VRF multicast & leaking** — PIM (IPv4) and PIM6 (IPv6) with domain control
  flags empty / one / several; every rendezvous-point mechanism (static, auto,
  bootstrap, fabric RP) with per-mechanism control flags; ASM / SSM patterns,
  resource, inter-VRF, stripe-winner; IGMP with SSM translation. Inter-VRF route
  leaking: internal / external leaked prefixes with length bounds, leaked
  subnets across both visibility scopes, a fallback-route group.
- **Bridge domain (`fvBD`)** — the full valid forwarding cartesian: unknown-unicast
  action (proxy / flood) x ARP flooding (on / off) x unicast routing (on / off) x
  multi-destination action (bd-flood / drop / encap-flood) x unknown IPv4/IPv6
  multicast action (flood / opt-flood) x endpoint move-detection (GARP / off) x
  IPv6-multicast-allow x limit-IP-learn-to-subnets — 360 routed and layer-2 BDs
  across the hardware-proxy, flood, and routing-off slices. Every other boolean
  forwarding knob covered both ways. Type regular and Fibre-Channel; legacy mode.
- **Subnets (`fvSubnet`)** — route scope (private / public / public+shared /
  private+shared) x subnet control (unspecified / querier / no-default-gateway /
  ND), across IPv4 and IPv6, with data-plane learning and the preferred /
  virtual flags. Endpoint children: anycast, NLB (unicast and IGMP modes),
  Microsoft network configuration; outside and ND-prefix bindings.
- **EPG (`fvAEPg`)** — 112 EPGs across the cartesian of QoS class (level1..level6 +
  unspecified) x match criteria (All / AtleastOne / AtmostOne / None) x
  enforcement preference x preferred-group membership, spread over six application
  profiles, with flood-on-encap, forwarding controls and shutdown covered per
  value. Full relation set bound (BD, custom QoS, data-plane policing, QoS
  requirement with ingress/egress policers, trust control, monitoring). Children:
  subnets, static endpoints (silent-host / tep / vep) with static IPs, virtual
  IPs, static paths, Fibre-Channel paths.
- **Static paths (`fvRsPathAtt`)** — deployment immediacy (immediate / lazy) x
  tagging mode (native / regular / untagged), data-driven onto a discovered
  leaf's ports, each on its own VLAN in the lane `vlan-2500..2599`. Leaf-port
  children: port security, IGMP / MLD snoop static and access groups, NLB static
  group.
- **Micro-segmentation (`fvCrtrn`)** — criteria across both matching-rule types
  (any / all); IP attributes (explicit address and use-subnet), MAC and DNS
  attributes; VM attributes across **every attribute type x every operator**;
  nested sub-criteria; the uSeg-BD association.
- **ESG (`fvESg`)** — 112 ESGs across the cartesian of QoS class x enforcement
  preference x match criteria x preferred-group membership, spread over four
  application profiles, with shutdown covered per value; the mandatory VRF scope
  and a custom-QoS bind. Every selector kind: IP/endpoint match expressions, an
  EPG selector, tag selectors across every value operator (equals / contains /
  regex). Tag selectors must be unique per VRF, so the selector set is attached
  to a single representative ESG.

## APIC combination constraints discovered live

Combinations the schema does not flag but the controller rejects — encoded or
avoided so the suite stays green:

- **VRF enforcement direction `mixed` is not user-settable** — the controller
  derives it; only ingress / egress can be set explicitly.
- **A leaked prefix's `le` must be strictly greater than the prefix length.**
- **A fallback-route group accepts only one fallback route** (members may be
  many).
- **Unicast routing off requires ARP flooding on** — a BD cannot disable both.
- **Hardware-proxy unknown-unicast cannot combine with encap-flood**
  multi-destination action.
- **Flood-on-encap and micro-segmentation cannot coexist on the same BD** — an
  EPG that floods on encapsulation excludes uSeg on its bridge domain.
- **A Fibre-Channel (`type=fc`) BD must have unicast routing disabled.**
- **A `/32` (or `/128`) subnet cannot be a default gateway** — it needs the
  `no-default-gateway` control.
- **Anycast and NLB endpoints require a `/32` host subnet.**
- **NLB endpoints (`fvEpNlb`) are accepted only under an EPG subnet**, not a BD
  subnet.
- **An NLB static-group MAC must be a multicast MAC** (NLB `03:bf:..` form).
- **IPv6 subnets cannot be virtual, must carry a non-zero host, and are the only
  place ND controls / ND prefix policies are valid.**
- **A uSeg criterion cannot set match precedence when it carries IP/MAC
  attributes.**
- **uSeg criteria are BD-scoped only** — VRF scope is unsupported.
- **DHCP relay mode `not-visible` is unsupported** on this platform.
- **A TEP/VEP static endpoint cannot carry multiple IP addresses** — extra static
  IPs belong only on a silent-host endpoint.
- **Tag selectors must be unique per VRF** — the same `(key, value)` cannot repeat
  across ESGs sharing a VRF.

## Skipped / abandoned, and why

- **VRF route-control-profile bind** (`fvRsCtxToRtctrlProfile`) — cloud-APIC
  only ("rtctrl Policies for FvCtx are only supported on cloud APIC Platform").
  The route-control profile object itself is created; the VRF bind is not.
- **Identity-group attribute** (`fvIdGroupAttr`) — its selector must be a
  cloud/NDO endpoint-group DN, which does not exist on an on-prem fabric.
- **PTP on a static path** — requires a global (fabric-level) PTP profile before
  an interface PTP profile can be selected; a fabric-phase dependency.
- **EPG domain attachment** (`fvRsDomAtt`) — needs an access / VMM domain, which
  lives in the fabric-access phase; exercised there.
- **ESG lif-ctx selector** — targets a service-graph logical interface context
  (services phase).
- **Contract-facing children** — provide / consume, the six contract labels,
  imported / taboo contracts, and contract-master inheritance belong to the
  contracts phase.
- **Coverage gaps (curated, but not reachable through the DSL today):**
  - **EPG static leaf** (`fvRsNodeAtt`) — resolvable in the reference map but not
    exposed on the EPG cursor's bind surface. Static *paths* work; static
    *leaves* do not.
  - **BD / subnet route-control profile** (`fvRsBDToProfile`,
    `fvRsBDSubnetToProfile`) — composed references, not exposed as `bind()`
    aliases.
