# 07 — L4-L7 services (service graphs + VMM)

Exhaustive provisioning of the L4-L7 services domain: abstract service graphs,
logical device clusters, device contexts, function profiles, policy-based redirect,
and VMware VMM domains. The goal is to prove the SDK can **express every
configuration knob** in this domain — every enum value, both states of every
boolean, every Flags combination, and the cartesian of the fields that interact —
by building the objects and pushing them to a live APIC. These are coverage sweeps,
not production designs; values are illustrative.

```bash
uv run pytest tests/integration/07_services -m integration -s
```

> These push ACI-side configuration. A working service data path additionally needs
> a real appliance behind the logical device and a reachable vCenter behind the VMM
> domain — see the parent [`../README.md`](../README.md) on hardware-dependent
> integrations. Each file owns a manual `wipe()`; the suite never tears down.

## At a glance

| Metric | Value |
| --- | --- |
| Files | 13 |
| Test functions | 18 |
| Config objects pushed | 651 |
| Live result | 18 / 18 accepted by the 6.0(9c) simulator |
| Config faults | 0 — every accepted object is config-clean |
| Residual faults | deployment-layer only — F2247 infra-PG on the `configure_infra_port_group=yes` VMM domains (see below); clears once the fabric infra-VLAN-on-AEP scaffolding exists |

The sweeps are sized to actually exercise the combination space — for example 72
abstract-graph templates (UI-template × scope × filter), 13 logical device clusters,
32 logical-interface contexts (the full 5-boolean cartesian), 24 VMM domains, the 20
enhanced-LAG load-balancing modes, and 16 EPG / custom-EPG aggregators. Objects are
spread across dedicated tenants (`niwaki-it-svcgraph`, `-svcnode`, `-svcconn`,
`-ldev`, `-devctx`, `-fprof`, `-devsup`, `-pbr`) and named VMware VMM domains under
`uni/vmmp-VMware`. Nothing touches a `default` object.

## What the suite covers

Each major object is swept across the axes that matter for it:

- **Service-graph templates** (`test_001`) — the full cartesian of UI template type ×
  `svc_rule_type` (epg / subnet / vrf) × `filter_between_nodes` (allow-all /
  filters-from-contract).
- **Function nodes + connectors** (`test_002`) — a node per function-template type,
  sweeping function type, routing mode, and the managed / share-encap flags; every
  `vnsFuncConnType` and both `att_notify` states on the function connectors; a
  dedicated unmanaged node and a copy node with a copy connector; and the abstract
  folder → param / relation config model on a connector.
- **Connections + terminals** (`test_003`) — the cartesian of adjacency type ×
  connection direction × connection type, with `direct_connect` / `unicast_routing`
  rotated; consumer and provider terminal nodes with in / out terminals and
  terminal connectors.
- **Logical devices** (`test_004`) — both declarable device types (physical,
  virtual), every function type and service type, both tenancies, and the managed /
  promiscuous / trunking / copy booleans; each with a concrete device (interfaces +
  params), credentials, logical interfaces, and a management interface sweeping
  every IP-allocation type and both in-band states.
- **Device contexts** (`test_005`) — device contexts per contract / graph / node
  selection, with the logical-interface contexts swept across the **full 5-boolean
  cartesian** (`acl` × `l3_dest` × `permit_handoff` × `permit_log` × `rule_type`),
  each binding a bridge domain and carrying a virtual IP; router configs.
- **Function profiles** (`test_006`) — container → groups → profiles → device /
  function / group configs; the instantiated config model (policy container +
  folder instances, tenant folder instances) swept across every `scoped_by` value
  and every `cardinality`, with `locked` / `mandatory` both ways.
- **Device support** (`test_007`) — device managers and chassis (credentials +
  management interface across a spread of server ports); firewall parameters
  (`vnsFWReq`) covering every named protocol value and a range of ports.
- **PBR redirect** (`test_008`) — the redirect policies swept across hashing
  algorithm × threshold-down action for L3 destinations, rotating the anycast /
  resilient-hashing / source-MAC-rewrite / threshold / local-pod booleans, plus L1
  and L2 policies; backup policies with several destinations, multiple redirect
  health groups, both service-EPG preferred-group states, and multiple destinations
  per policy spread across the health groups.
- **VMM** (`test_009`–`test_013`) — domains across the full cartesian of access mode
  × encap mode × endpoint-inventory × default-encap (with control-knob Flags and the
  retrieval booleans rotated); controllers across every DVS version with stats / N1KV
  / VXLAN-preference rotated, cluster controllers, and host-availability with a
  desired state per host status; vSwitch policy groups with the full
  load-balancing-mode × LACP-mode enhanced-LAG sweep; uplink containers and uplinks;
  EPG / custom-EPG aggregators across immediacy × allocation × classification
  cartesian with the config-mode settings and feature Flags, each with an encap range
  in the VMM VLAN lane (vlan-2700..2799); and the domain / vSwitch interface-policy
  override bindings.

## Mutually-exclusive constraints — factored, not skipped

Several APIC rules forbid two settings from coexisting on one object. Rather than
picking one side, the suite puts **each side in its own object** so both are covered.
The pairs, and where each lands:

- **Managed vs unmanaged** — managed clusters carry credentials and a management
  interface; unmanaged clusters (L1/L2 and copy) omit them. Both are declared.
- **Physical vs virtual** — separate clusters; the virtual ones additionally carry
  the promiscuous / trunking booleans (both states), which only apply to virtual.
- **Copy vs regular** — a copy device is its own cluster (unmanaged, physical,
  function type `None`, service type COPY), separate from the managed regular devices.
- **L1/L2 vs L3 function types** — L1/L2 devices are their own physical, unmanaged
  clusters; the L3-style function types (GoTo / GoThrough / None) are managed clusters.
- **Encap on the logical interface vs the concrete interface** — encap lives on the
  logical interface for the (non-active-active) clusters here; the concrete-interface
  placement requires active-active, which is gap-blocked (see below).
- **Anycast vs tracked / located redirect** — anycast redirect policies drop IP-SLA
  tracking, health groups, and location-aware PBR; the non-anycast policies exercise
  all three. Both kinds are declared.
- **Backup policy vs resilient hashing** — the backup-policy relation is attached
  only on the policies that enable resilient hashing; the others leave both off.

## Skipped / abandoned, and why

What remains uncovered is genuinely uncoverable — either the fabric can't take it, or
the DSL has no reachable maker/bind for the required relation. Reported in a
`# COVERAGE GAPS:` block at the top of each file; never forced with a raw `.mo()`.

### Blocked by a coverage gap (no reachable maker/bind)

- **Active-active mode** (and with it encap on the concrete interface) — an
  active-active L1/L2 device requires a logical-interface domain
  (`vnsRsLIfDomP` → `physDomP`), and that relation has **no curated bind**. So
  `active_active_mode` stays False and encap stays on the logical interface.
- **Enhanced-LAG policy name** on a logical interface — the APIC requires the VMM
  domain that owns the enhanced-LAG policy to be **associated to the device**
  (`vnsRsALDevToDomP` / `vnsRsALDevToPhysDomP`), and that association has no curated
  bind. So the enhanced-LAG name is omitted.
- **The `firewall` bind → `nwsFwPol`** on a VMM domain and its vSwitch group has no
  declarable target anywhere in the vocabulary.
- **L1 / L2 redirect destinations** (`vnsL1L2RedirectDest`) have no maker; the L1/L2
  redirect policies carry no destination.
- **Deep folder / param nesting** — the abstract folder and the instantiated folder
  instance recurse on the APIC, but the DSL exposes one level; folder-in-folder and
  the folder/param model under a function-profile config are gaps.
- The standard per-object `vnsRs*` relations (device-to-EPG, meta-device / meta-
  function, chassis / device-manager EPG relations), secret credentials, and the
  universal `tag` / `annotation` children have no typed maker on these cursors.

### Rejected by this environment (valid config the SDK expresses, but the fabric can't take)

- **Endpoint attestation** (`vmmEpValidatorPol`) — rejected by this controller
  (*"Endpoint Attestations are not supported by this Controller"*). Left out with a
  note at the maker call; re-enable against a controller that supports it.
- **The VMM multicast-address-namespace bind** (`vmmRsDomMcastAddrNs`) is rejected
  outright by the APIC for VMM domains, so it is not bound.
- **AVE-only knobs** — `enable_ave_mode`, host-availability monitoring, the `ivxlan`
  encap and `hw` switching preference (they imply AVE), and `arp_learning` enabled.
  Out of scope on a plain DVS simulator.
- **Cloud device type** (`CLOUD`) and the `CLOUD_*` function-template types — need a
  cloud APIC.
- **vCenter-dependent operation** — the VMM controllers, the protected VM group, and
  inventory sync push their ACI-side config but need a reachable vCenter to form; the
  logical devices and copy service need a real appliance. Config is accepted; the
  operational data path is not exercised here.

### Deployment-layer residual fault (config accepted, deploy incomplete)

Both values of the VMM domain `configure_infra_port_group` axis are swept (the
child-closure rule keeps both). The APIC **accepts** the config on the domains that
enable it (`d00`, `d06`, `d08`, `d10`, `d12`, `d16`, `d20`, `d22`), but each raises a
deployment-layer **F2247** — *"Infrastructure VLAN needs to be configured for Infra-PG.
Please enable under AEP."* The infra port-group cannot fully deploy until the fabric
carries an infrastructure VLAN enabled on an AEP tied to the domain, which is
fabric-access / day-0 scaffolding outside this isolated services sweep. It is a
deployment artifact, not a config-expression defect — the fault clears once that
scaffolding exists (marked with an inline `# ENV FAULT` note on the sweep). The
aggregator-domain trunk-portgroup fault the earlier sweep carried is now fixed: the
aggregator domain binds a VLAN pool covering the aggregator lane, and that F2247 is
gone.

## APIC combination rules discovered live

Pushing the sweeps surfaced these config rules; each is now encoded (valid
combinations generated) or avoided (invalid ones excluded), so the suite pushes clean:

1. Enhanced-LAG policy name is valid only on a **virtual** device.
2. `encap` on a concrete interface is accepted only in **active-active** mode.
3. `encap` on a logical interface is rejected in active-active mode (exclusive with #2).
4. A **copy device** must be **unmanaged**, **physical**, and **function type `None`** —
   and its function node must be unmanaged too.
5. **L1 / L2** devices must be **physical** and **unmanaged**.
6. **Active-active** mode is valid only on an L1 / L2 device.
7. `vnsFWReq` `consumer` / `provider` accept only device-package connector tokens.
8. **L3 connections** require **`unicast_routing` on**.
9. **Anycast** redirect policies cannot carry IP-SLA tracking or a health group.
10. **Location-aware PBR** and **anycast** are mutually exclusive.
11. The **backup-policy relation** requires **resilient hashing enabled** — so anycast
    and resilient hashing are driven independently to reach that shape.
12. A **backup policy is referenced by only one redirect policy** — each resilient
    policy that needs one gets its own backup policy.
13. `arp_learning` enabled on a VMM domain is AVS/AVE-only.
14. Each **enhanced-LAG policy reserves DVS uplinks** from a finite per-domain pool —
    the load-balancing-mode sweep is spread four modes per domain across five domains.
15. A **NetFlow VMM exporter** collector L4 port cannot be the "unspecified" default —
    a real port is set.

Text fields also enforce character patterns: `vnsFWReq.ace` / interface names reject
spaces and `/`, and the interface-context description rejects `=`.

One schema quirk worth noting: the connection-direction value `unknown` and `provider`
map to the same wire value in the pinned schema, so a connection declared with
`conn_dir="unknown"` reads back as `provider` — that axis is effectively two-valued on
the wire.
