# 09 — Management

In-band and out-of-band management, driven end to end through the SDK against a
live APIC. The suite provisions the management world the way an operator would —
in-band and out-of-band management EPGs and their contracts, the labels that
classify them, endpoint tags, and address pools — and pushes **every combination
of configuration the SDK can express** for each object, to prove the controller
accepts the full surface.

```bash
uv run pytest tests/integration/09_management -m integration -s
```

These are exhaustive, illustrative walkthroughs, not production configuration —
see [`../README.md`](../README.md) for what that means. Everything is named
`niwaki-it-*`; the APIC-managed `mgmt` tenant and its `mgmtp-default` /
`extmgmt-default` singletons are only traversed, never reconfigured. Each file
owns a manual `wipe(aci)` (never run by the suite).

## Stats

| Metric | Value |
| --- | --- |
| Files | 9 |
| Test functions | 9 |
| Config objects pushed | ~1050 |
| Live result | 9 / 9 pass, idempotent (a second run re-pushes clean) |
| Faults | 0 |

## What the suite covers

Each file drives one slice of the management world and walks the full
combination space of its objects:

- **`test_001_inband_epgs`** — in-band EPGs (`mgmtInB`) over the cartesian of
  `flood_on_encap` (2) × `preferred_group_member` (2) ×
  `provider_label_match_criteria` (4), with the QoS class rotated over all seven
  priorities. Each EPG takes a unique encapsulation VLAN from the 2900–2999 lane
  and binds a shared in-band bridge domain.
- **`test_002_inband_subnets`** — one in-band EPG carrying a wall of subnets:
  every `scope` flag combination × every `subnet_control` flag combination, with
  `ip_dp_learning` and `virtual` flipped across the set.
- **`test_003_inband_labels`** — one in-band EPG wearing **all 140 policy
  colours** the `tag` enum offers, distributed across the six label makers
  (provider / consumer, provider-subject / consumer-subject, provider-contract /
  consumer-contract), with `complement` flipped both ways on the three label
  kinds that expose it.
- **`test_004_inband_contracts`** — a matrix of regular contracts (`vzBrCP`)
  across every `scope`, with QoS class and contract DSCP rotated; each with a
  subject whose match types, `reverse_filter_ports` and DSCP vary, bound to
  filters spanning several ether-types and protocols. The in-band EPG then binds
  its bridge domain, imported-contract interface and taboo contract, and
  provides / consumes across the matrix.
- **`test_005_oob_contracts`** — out-of-band contracts (`vzOOBBrCP`), one per
  target DSCP (all 23), with `intent`, `scope` and QoS class rotated; each with a
  subject varying match types, reverse and DSCP, and — across a spread of
  subjects — ingress/egress terminals, subject labels and exceptions.
- **`test_006_oob_epgs`** — out-of-band EPGs (`mgmtOoB`) and external management
  networks (`mgmtInstP`, under the `extmgmt-default` entity), one of each per QoS
  priority; the EPGs provide, and the external networks (with imported subnets)
  consume, the out-of-band contract.
- **`test_007_endpoint_tags`** — MAC (`fvEpMacTag`) and IP (`fvEpIpTag`) endpoint
  tags, factored across several dedicated tenants (each with its own `fvEpTags`
  container) and, within each, spread over its VRFs and bridge domains with
  distinct ids. Every tenant classifies into a user-created security domain
  through a domain reference (`aaaDomainRef`).
- **`test_008_address_pools`** — IP address pools (`fvnsAddrInst`) over the
  cartesian of `address_type` × `skip_gw_validation`, each with several unicast
  address blocks, plus the IP address management pool (`fvAddrMgmtPool`) with its
  blocks — factored across several dedicated tenants, each referencing a
  user-created security domain.
- **`test_009_security_domains`** — user-created security domains (`aaaDomain`)
  with `restricted_rbac_domain` set both ways, and tenants that bind domain
  references (`aaaDomainRef`) to them: one tenant per domain, plus a tenant that
  references several at once (single- and multi-reference shapes both).

## Combination constraints, and how the suite covers around them

Pushing the full cartesian surfaces the controller's own rules. Where two values
cannot coexist on one object, the suite **factors** them — each side onto its own
object — so both are still exercised. These were all discovered against the live
APIC:

- **A subnet scope cannot be both `private` and `public`.** Those flags are
  mutually exclusive, so no subnet carries both — but separate subnets carry
  `private` and `public`, so each value is covered.
- **A contract (`vzBrCP`) can only be created in `install` intent** (the estimate
  modes are a transient two-phase state the controller refuses on a fresh
  contract). The estimate intents are covered on the out-of-band contracts, which
  do accept them at creation — so the intent enum is exercised across the two
  contract classes.
- **A domain reference cannot point at a built-in security domain** (`mgmt`,
  `common`, `infra`). Instead the suite creates **user-defined** security domains
  (`aaaDomain`) and binds the tenant domain references (`aaaDomainRef`) to those —
  so the domain-reference coverage is kept, not dropped (`test_007`–`test_009`).
- **A management EPG subnet cannot be `preferred`** (a preferred subnet is only
  valid under a bridge domain). Every in-band EPG subnet is left non-preferred;
  the preferred flag is covered on bridge-domain subnets in the tenant
  walkthroughs.
- **Each in-band EPG needs a unique encapsulation.** Two in-band EPGs cannot share
  an access encap, so the suite draws a distinct VLAN per EPG from the 2900–2999
  lane.

## Genuinely uncoverable here, and why

- **Security inheritance and intra-EPG contracts** — `contract_master`
  (`fvRsSecInherited`) and `intra_epg` (`fvRsIntraEpg`) attach only to application
  EPGs, ESGs and external-network EPGs; the object model does not allow them on a
  management EPG, so they belong to the tenant / contract walkthroughs.
- **Per-node management addressing** (`static_route` / static-node bindings).
  Assigning a management IP to each switch is day-0 node bring-up, covered in
  `01_day0`; a switch can belong to only one in-band and one out-of-band
  management EPG, so repeating it against these named EPGs would put the fabric's
  nodes in two management groups at once — the controller rejects it.
- **Subnet endpoint children** (anycast endpoint, NLB endpoint, SCVMM network
  config) hang off `fvSubnet` but are application-EPG subnet features, covered
  under the tenant walkthroughs; they are inappropriate on a management subnet.
- **Subject service-graph attachment** needs a service graph and is covered under
  the services walkthrough.
- **Coverage gaps.** A few `mgmtInB` children are not reachable through the
  curated vocabulary and are left uncovered: the MSC / orchestrator glue
  (`fvOrchsInfo`, `mdpClassId`, `orchsLDevVipCfg`) and the L4-L7 service-graph
  attach family (`vnsAbsFolder`, `vnsAbsParam`, `vnsAbsCfgRel`, `vnsFolderInst`,
  `vnsParamInst`, `vnsCfgRelInst`).
