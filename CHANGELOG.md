# Changelog

All notable changes to this project are documented here.  The format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow semver
(0.x — the API may still change between minor versions).

## [0.14.0] — 2026-07-15

L4-L7 service graphs join the vocabulary — the last large domain of the ACI
configuration plane.

### Added

- **Service graph templates.**  `tenant(name).service_graph(...)` with function
  nodes (+ function connectors carrying config folders and parameters, copy
  connectors), connections, and consumer/provider terminal nodes.
- **Logical devices.**  `logical_device` (the L4-L7 cluster) with concrete
  devices (+ their interfaces and parameters), logical interfaces, credentials
  and management interface; the graph's function node binds a logical device.
- **Device context.**  `logical_device_context` (keyed by contract/graph/node)
  selecting a device and router configuration, with per-connector interface
  contexts mapped to bridge domains and their virtual IPs.
- **Function profiles.**  Profile container → group → profile with function,
  device and group shared configs, and the abstract folder/parameter model.
- **Device manager, chassis and instance config.**  Device manager and chassis
  (with credentials), the deployed L4-L7 policy container with folder/parameter
  instances, and normalized firewall parameters.

Service graphs define the ACI-side topology and configuration; rendering a graph
needs a real L4-L7 appliance — see the "Hardware-dependent integrations" note.
The device-package metamodel and normalized LB/NAT requests stay uncurated.

### Coverage

The declarable config plane across the five domains (operational, diagnostic and
out-of-scope families — cloud, multi-site/NDO, device-package meta — excluded):

| Domain            | Declarable | Curated |    % |
| ----------------- | ---------: | ------: | ---: |
| Tenant            |        368 |     318 | 86 % |
| Access (`infra`)  |        164 |     141 | 85 % |
| Fabric            |        179 |     145 | 81 % |
| Controller        |         20 |       9 | 45 % |
| VMM               |         22 |      15 | 68 % |
| **Global (union)**|    **753** | **628** | **83 %** |

790 curated positions across 652 distinct classes.

## [0.13.0] — 2026-07-15

VMM domains join the vocabulary, and the push engine learns to fold the
plugin-managed path prefixes they need.

### Added

- **VMM domains.**  `design().vmm_provider(vendor).vmm_dom(name)` with its
  vCenter/SCVMM controller (cluster controller, host-availability with
  protect-VM group and host-desired-state, EP-validator), credentials, vSwitch
  policy group (enhanced LACP) and uplink container/policies, plus the domain's
  EPG aggregators.  The domain and vSwitch container bind their default
  interface policies and pools; the AAEP's abstract `domain` bind now resolves
  to a declared VMM domain, closing the access-domain loop.
- **Carrier classes.**  A curated `carrier` set names non-creatable, path-only
  classes the APIC rejects on a standalone POST or `rsp-subtree` read (a VMM
  provider, `uni/vmmp-VMware`).  The staged push emits no op for them — their
  declared children post at their full DNs and the APIC materialises the path —
  and the plan diffs those children instead.

### Notes

- Pushing a VMM domain lands the APIC-side config and re-plans cleanly, but a
  reachable vCenter / SCVMM controller is required before inventory syncs — see
  the new "Hardware-dependent integrations" note in the design-first guide.  The
  VMM orchestrator provider (NDO) stays out of scope.

## [0.12.0] — 2026-07-15

The fabric-policy (`fabric`) and controller (`controller`) domains join the
tenant and access-policy planes as first-class, live-verified vocabulary, and
non-creatable default singletons finally read as the singletons they are.

### Added

- **Fabric policies (six waves).**  The fabric-internal ports charpente
  (leaf/spine switch, interface and module profiles, selectors, policy groups,
  pod profile); fabric interface and protocol policies (link-level, link-flap,
  L3, L2 MTU, MACsec fabric, ISIS, COOP, fabric VXLAN, vPC domain, PSU
  redundancy, WWN, load-balance, ZR/ZRP/DWDM optics, node control); fabric
  monitoring (callhome/SNMP/TACACS destination groups, SNMP policy, fabric and
  common monitoring policies with their sources); system and global policies
  (communication services, geo-location hierarchy, proxy, datetime format,
  connectivity preference, admin-down, deployment, out-of-service); firmware,
  maintenance and config management (policies, groups, catalogs, export/import/
  snapshot/rollback, scheduler, license); telemetry, analytics, TWAMP, core/
  techsupport export, latency modes and fabric VSPAN.
- **Controller policies.**  Cluster, audit-log retention, controller firmware
  and maintenance, DRR, fabric first-time setup (+ per-pod), scheduler, cores
  and CIMC-node policies, alongside the existing fabric membership.
- **Singleton-aware makers.**  APIC creatability is baked into every generated
  model (`_is_creatable`).  A maker whose target is a non-creatable, name-only
  default singleton now defaults its name to `"default"` — `.qos_instance_
  policy()`, `.communication_policy("default")`, `.coop_group_policy()` read as
  the singletons they are, configuring the existing instance in place.  Spanning
  Tree (MST) rejoins the access vocabulary on this basis.

### Notes

- Deprecated or feature-restricted classes are omitted where the 6.0 APIC
  rejects them (telnet service, telemetry server groups, SD-WAN SLA).  Kafka
  policy and multi-domain (NDO) stay out of scope.

## [0.11.0] — 2026-07-15

The access-policy (`infra`) configuration surface is now substantially complete.
The fabric's physical side — pools, domains, policy groups, interface and switch
policies, QoS and control-plane protection, fabric-wide system settings, and
observability — is first-class vocabulary, typed and live-verified against a
6.0(9c) fabric.

### Added

- **Pools and the Fibre Channel domain.**  VXLAN, VSAN and multicast-address
  pools with their ranges; the FC domain binding its VLAN/VSAN/address pools and
  VSAN attributes.
- **Policy groups and profiles.**  Leaf/spine switch groups, the spine access
  group, PC/vPC override, the FC port/PC/PC-override groups, breakout group and
  modular-card group; the spine interface profile with its port selector; FEX,
  pod and access-module profiles with their selectors and blocks.
- **Interface policies.**  L2 interface, LACP member, PoE, FC, MACsec (container
  with parameters/keychain/key policies), SyncE, link-flap and 802.1x
  port/node authentication; the PoE/FC/SyncE instance and fabric policies.
- **QoS and control-plane protection.**  The QoS instance policy and its six
  classes with per-class buffer, congestion, priority-flow-control, queue,
  schedule and microburst policies; interface LLFC/PFC/slow-drain; CoPP
  leaf/spine and per-interface policies; the CoPP prefilter with its ACL entries.
- **Fabric-wide and system policies.**  CP/controller MTU, TCP MSS, fabric-wide
  settings, port tracking and status, forwarding-scale profile, USB
  configuration, fast link-failover, flash configuration, remote-leaf pod
  redundancy, system GIPo, infrastructure zoning; DHCP relay node/pod groups,
  node/pod management addresses and the managed-node connectivity group.
- **Observability and timing.**  The monitoring policy with syslog/SNMP/callhome/
  smart-callhome/TACACS sources and fault/event severity assignment; PTP node
  policy, profile, domain and template; the four global BFD policies; the
  NetFlow node policy; VSPAN sessions and destination groups.
- **Policy-group wiring.**  The interface and switch policy groups now bind every
  relevant policy above (CoPP, QoS, MACsec, BFD, PTP, monitoring, and the rest).

### Notes

- Non-creatable fabric singletons — the QoS instance/classes, CP MTU, TCP MSS,
  fabric-wide settings, port tracking/status, system GIPo, and the zoning
  profile — are configured in place through their makers rather than created.

## [0.10.0] — 2026-07-15

The tenant's configuration surface is now substantially complete.  A large body
of tenant protocol and policy configuration that previously needed the `.mo()`
escape hatch is first-class vocabulary — typed makers, per-position reference
pages, live-verified against a 6.0(9c) fabric.

### Added

- **Multicast.**  VRF-level PIM (`.pim`), IPv6 PIM (`.pim6`) and IGMP (`.igmp`)
  with the full RP, pattern and filter policies; BD-level PIM with its route-map
  filters; the PIM/IGMP interface-policy filters; IGMP/MLD snooping groups.
- **Route-control and leaking.**  Route-map `match_*` and `set_*` clauses on the
  match rule and action profile, inter-VRF route leaking (`leak_routes`), and
  static routes with their next hops.
- **L3Out.**  External connectivity labels, node loopbacks and infra nodes,
  path-level forwarding and rogue-exception MAC, VRF validation and a global VRF
  name.
- **Security and VPN.**  Host protection (microsegmentation) with its subject →
  rule tree, and site-to-site IKE/IPsec (`isakmp_*`, `ipsec_phase1/2`); port
  security on a static path.
- **Protocol policies.**  DNS server groups, tenant AAA server groups, SNMP
  contexts and communities, QoS class mappings, Fibre-Channel uplink pinning,
  SR-MPLS node SIDs and SRGB, ND RA subnets, HSRP secondary VIPs, micro-BFD,
  PTP, BGP data-plane, DHCP relay gateway IP, and virtual SPAN.
- **Endpoints and pools.**  Anycast and NLB endpoints, IP address-management
  pools, VRF route summarization and deployment, uSeg BD associations, ESG
  LIfCtx selectors.

Curation coverage of the tenant's declarable configuration rose from roughly 40%
to 85%, and the reference now documents 462 curated positions (up from 293).

### Notes

- Multi-site / intersite objects (managed by Nexus Dashboard Orchestrator),
  Cloud APIC classes, orchestrator-injected config, learned endpoints and L4-L7
  service graphs remain out of the tenant vocabulary by design; the `.mo()` and
  `bind_dn()` escapes keep the rest of the 2,222 generated classes one call away.
- A handful of tenant classes the APIC auto-manages and refuses to create
  (`extdevSDWanPolCont`, `fvConnInstrPol`) are deferred until the push engine
  can upsert such carriers.

## [0.9.0] — 2026-07-15

The models now carry the **right Python type** for every field, and the
type checker sees it.  Before this release the code generator knew only a
handful of schema types and quietly rendered everything else as `str`; that
made numbers into text, made bitmasks unusable, and hid the readable field
names from your IDE.

### Changed

- **Numbers are numbers.**  A field the schema declares numeric is now `int`
  or `float`, with the schema's own bounds — not a string.  A field the APIC
  stores under a *name* (a filter port, a BGP stale interval) accepts the
  number and canonicalises the way the APIC does: `vzEntry(destination_from_port=80)`
  round-trips as `"http"`, so `push(mode="plan")` finally converges on it.
- **A bitmask is a set.**  A field that is a subset of a closed set (a subnet
  `scope`, `vzEntry` `tcp_rules`, a LACP `ctrl`) is now `frozenset[SomeEnum]`.
  It accepts everything reasonable — the wire string, a set of names, a set of
  members, a single flag — and order never matters, so the phantom drift the
  APIC's own re-ordering used to cause is gone:

  ```python
  fvSubnet(ip="10.1.1.1/24", scope="public,shared")        # the wire form
  fvSubnet(ip="10.1.1.1/24", scope={"public", "shared"})   # a set of names
  vzEntry(name="ssh", tcp_rules="syn,ack")                 # was rejected before
  ```

- **Addresses are validated.**  IPv4 and IPv6 fields carry an address pattern
  instead of accepting any string.
- **Readable names reach your IDE.**  A renamed field (`arp_flooding` for
  `arpFlood`) is now accepted *by that name* by type checkers — Pylance and
  pyright no longer flag `fvBD(name="web", arp_flooding=True)` while accepting
  the wire spelling.  The wire name still works on reads and in query filters.

### Added

- `niwaki.query(cls)` is overloaded: a model class in gives typed instances
  out; a class *name* string in gives base `ManagedObject`s (their attributes
  in `model_extra`).
- `ref()` is accepted by `bind_dn()` and by the contract verbs, not only by
  `bind()`.
- The generated reference now documents every field's real type and, for an
  enum or a set of flags, its allowed values and default.

### Migration

- A field you *read back* may now be a number or a `frozenset` where it used
  to be a string — compare against `80`, not `"80"`, and against
  `{"public", "shared"}`, not `"public,shared"`.  Construction is unchanged:
  the wire string is still accepted everywhere.
- A bitmask default is a `frozenset`; a numeric field's default is a number.
- One long-standing default was corrected: `bgpBestPathCtrlPol.ctrl` defaulted
  to `as-path-multipath-relax` **enabled** and now defaults to no flags, as the
  schema declares.  `ospfExtP` regained its `area_ctrl` field, previously
  dropped by a name collision.

### Internal

- pyright now type-checks the whole repository (the generated tree excepted) in
  the commit gate and CI, alongside mypy — it reads the constructor signature
  Pydantic synthesises, which mypy does not.  A cold-start budget and a
  documentation type-column guard were added.

## [0.8.0] — 2026-07-14

### Added

- **The EPG/ESG world enters the vocabulary.**  An application EPG now reaches
  everything the APIC hangs under it:
  `subnet()` (with its `l3out` and `nd_ra_prefix_policy` binds),
  `static_endpoint()` (plus `static_ip()`, and the path/node it lives on),
  `criterion()` — the uSeg selector, with `ip_attribute()`, `mac_attribute()`,
  `vm_attribute()`, `dns_attribute()` and nested `sub_criterion()` —
  `virtual_ip()` for L4-L7 VIPs, and `fc_path()` for Fibre-Channel paths.
- **Endpoint security groups**: `app().esg()` with its selectors
  (`ep_selector()`, `epg_selector()`, `tag_selector()`), its mandatory
  `vrf` bind, and the contract verbs.
- New EPG binds: `contract_master` (contract inheritance — one alias, EPG or
  ESG alike), `imported_contract`, `taboo_contract`, `custom_qos_policy`,
  `dpp_policy`, `monitoring_policy`, `trust_control_policy`; and the tenant
  objects they point at: `taboo_contract()` (with its `subject()`),
  `imported_contract()` and `monitoring_policy()`.
- A third contract verb, `intra_epg()` (`fvRsIntraEpg`), on EPGs and ESGs.
  Contract verbs are now fully data-driven: curating one in the vocabulary is
  enough — the runtime no longer hardcodes the list.

- **The contract world completes** (229 curated positions).  `vrf().vzany()`
  arrives — contracts for a whole VRF, reached through relation classes of its
  own (`vzRsAnyToProv` / `vzRsAnyToCons` / `vzRsAnyToConsIf`), which the
  data-driven verbs absorb without a line of engine code.  A subject that stops
  applying both ways gets one filter per direction with `in_term()` and
  `out_term()`; `exception()` excludes an EPG from a contract, on the contract
  or on the subject; `oob_contract()` covers out-of-band (management) contracts
  with their own subjects.
- The six contract labels (`provider_label()`, `consumer_label()`,
  `provider_subject_label()`, `consumer_subject_label()`,
  `provider_contract_label()`, `consumer_contract_label()`) are curated
  wherever the MIT hangs them — EPG, ESG, vzAny, subject and external EPG — so
  the `provider_label_match_criteria` attribute finally has labels to compare.

- **`ref()` — a reference can configure the relationship itself.**  Most
  relations are pure edges, but 26 curated binds resolve to a class that
  carries fields of its own, and they were unreachable: the resolution
  immediacy of an EPG-to-domain attachment, the `directives` of a filter under
  a subject (this is where contract logging lives), the `direction` of a
  route-control profile, a node's management address, an ERSPAN collector's
  IP.  Wrap the target — `epg.bind(domain=ref("prod-phys",
  resolution_immediacy="immediate"))` — anywhere a plain name goes, including
  `bind_dn()` and the contract verbs.  The fields are validated against the
  relation class at declaration time.

- **Observability**: SPAN (`span_source_group()` with its sources, label and
  filter group; `span_destination_group()` with its destinations), NetFlow
  (`netflow_monitor()`, `netflow_exporter()`, `netflow_record()`) and QoS
  requirements (`qos_requirement()`, with `ingress_dpp()`/`egress_dpp()` and
  the EPG bind that reaches it).  SPAN and NetFlow are curated under the
  tenant, under `infra` and under `fabric` alike.
- **The L2 edge and management**: `l2out()` complete (node profile, interface
  profile, static path, external EPG with labels and contract verbs), the
  in-band and out-of-band management EPGs — which give the out-of-band
  contract someone to provide and consume it — endpoint tags (what an ESG
  `tag_selector` matches), IP address pools, and fallback route groups.
- **The closed world is closed**: every curated `bind()` now has a declarable
  target, except the ones the fabric discovers for you (`fabricNode`,
  `fabricPathEp`), which is what `bind_dn()` is for.  293 curated positions,
  up from 176 at the start of the wave.

### Changed

- **Renamed, on an L3Out external EPG**: the two subject-label makers were
  curated before those classes had a name of their own and carried a generated
  one.  They now speak the same word as everywhere else:
  `.vz_prov_subject_label(...)` → `.provider_subject_label(...)` and
  `.vz_cons_subject_label(...)` → `.consumer_subject_label(...)`.  Their
  reference pages moved with them.
- A verb's parameter is named after what it points at (`provide(contract)`,
  `ingress_dpp(dpp_policy)`), and its flavor is read off the relation class
  rather than assumed to be name-flavored.  Existing call sites are unaffected:
  `provide(contract)` keeps its exact signature.

## [0.7.0] — 2026-07-13

### Added

- The models now carry the APIC's own catalog of accepted-but-inconsistent
  configuration states: 98 classes declare a read-only `configIssues` enum
  in the schemas (~2,500 codes — `fvBD` alone lists
  `FHS-enabled-on-l2-only-bd`), previously invisible to users.  Each such
  class exposes `_config_issues` (`{code: description}`) and lists the
  codes in its docstring — the states your IDE can warn you about before
  the APIC flags them.  Descriptions come from the value's `comment`
  (rich prose) with the schema `label` as fallback — every code is
  described.
- Two more declared constraint channels reach the models: `_fault_codes`
  (659 classes, 739 F-codes — `fvBD` carries
  `fltFvBDMulticastEnabledOnL2BD`) and `_relation_info` on relation
  classes (cardinality, enforceable, resolvable).  Both are guarded by
  an anti-drift integrity suite that re-derives them from the raw
  schemas for all 2,222 generated classes.

- **The DSL reference**: the generated vocabulary book becomes a full
  reference — one page per curated position with an attribute table
  (parameter, wire alias, type, allowed values, default, Cisco's
  description), the children/binds/verbs it reaches, and the APIC
  diagnostics (config issues, fault codes) it can raise.  Plus a page of
  the 106 enums the vocabulary uses (each value with Cisco's meaning) and
  the read-side navigation vocabulary.  The typed keyword arguments of
  every maker — the SDK's core surface — were previously visible only by
  hovering in an IDE.

### Changed

- The transport boundary is public: `niwaki.transport` exports the four
  structural protocols (`MoWriter`, `MoReader`, and their async mirrors)
  and both sessions (`ApicSession`, `AsyncApicSession`) — the extension
  point the testing guide relies on no longer lives in a private module.
- The API reference renders objects under their real import path
  (`niwaki.Niwaki`, not `niwaki.facade.Niwaki`), no longer exposes the
  models' private ClassVars as public attributes, and gained the entries
  it was missing: the root package, `mo_diff` / `parse_imdata`,
  `REGISTRY`, the filter operators, and vocabulary navigation.
- Deep anchors of the vocabulary pages moved: each position now has its
  own page (`reference/vocabulary/tenant/tenant-bd.html`) instead of an
  anchor on the domain page.

## [0.6.0] — 2026-07-13

A professional documentation overhaul, and the enterprise-CA answer.

### Added

- `verify_ssl` accepts a **path to a CA bundle** (PEM) on `Niwaki`,
  `AsyncNiwaki` and both sessions — TLS verification against a private
  or enterprise CA no longer requires disabling verification (the
  bundle loads eagerly into an `ssl.SSLContext`; a wrong path fails at
  construction).
- Three documentation pages the adopting coder was missing: **Testing
  your automation** (payload asserts, plan as a convergence test, a
  fake APIC at the httpx boundary, transport-protocol stubs — all
  executable), **Compatibility & limits**, and **Troubleshooting
  connection & auth** (the exception → question → knob ladder).
- The documentation home is a real landing page (orientation, a
  "Start here" path, the Diátaxis compass), every guide page ends with
  next steps, and deletion semantics have a canonical section.

### Fixed

- The `plan` documentation described pre-0.3.0 behaviour: plan reads
  are scoped with `rsp-subtree-class` to the classes the design
  declares — the stale "avoid planning large domains" advice is gone,
  and the write-only-secrets caveat now lives where `plan` is taught.
- One maxim ("structure is literal, vocabulary is translated"), one
  term per concept, position counts generated straight from the
  vocabulary — the terminology and numbers can no longer drift.
- Cisco placeholder comments (the literal text "null", on 621 schema
  properties) no longer leak into maker Args sections and field
  descriptions — those fields simply stay undescribed.

## [0.5.0] — 2026-07-13

Cisco's own definitions, everywhere the IDE looks.

### Added

- The APIC schema comments — Cisco's human-written definitions, covering
  79% of configurable properties and 84% of classes — now flow through
  the entire generated surface:
  - every described model field carries `Field(description=...)`: IDE
    hover, Pydantic error context and Sphinx autodoc all show Cisco's
    definition;
  - model class docstrings carry the class definition;
  - enum members carry per-value docstrings (`OspfNwT.BCAST` —
    "Broadcast interface");
  - every DSL maker exposes a generated Args section: field definition,
    allowed enum values and non-empty defaults, straight from the
    schemas.
- Wire behaviour is untouched (golden payloads pass unchanged) and
  cold-start stays at ~90 ms — models remain lazily loaded.

## [0.4.0] — 2026-07-12

The vocabulary triples and the whole delivery pipeline matures.

### Added

- **L3Out, complete** (wave 1): node and interface profiles, node/path
  attachments as literal-DN makers, BGP peers with ASN and prefix
  policies, OSPF/EIGRP/HSRP/PIM/IGMP/BFD/MPLS interface profiles,
  floating SVIs, external EPGs with subnets and contract verbs,
  route-control profiles and contexts.
- **Tenant > Policies > Protocol, 28/28 GUI folders** (wave 1bis): BGP and
  EIGRP address-family contexts, OSPF timers, data-plane policing, DHCP
  relay (provider carries the server address) and options, endpoint
  retention, external bridge group profiles, First Hop Security with RA
  guard, IGMP/MLD snooping, IP SLA with ICMP/TCP probes, track
  lists/members, PIM route maps with entries, route tags, tenant-level
  route maps, keychains with key tables — plus the standalone L4-L7
  policies (PBR with destinations, backup, health groups, service EPG).
  The vocabulary grows from 57 to 176 curated positions.
- `propose_vocabulary` codegen tool: assisted-curation candidates
  (makers from the navigation jargon, binds from the reference map,
  contract-verb detection, review flags) — the vocabulary now grows in
  reviewed waves, and contributions need no fabric.
- Write-only schema properties (passwords, pre-shared keys) are tracked
  as `_secure_props` on the models and excluded from `plan`/diff
  comparison — a pushed secret no longer reports phantom drift.
  Consequence: rotating a secret requires a push; `plan` cannot see it.
- Documentation: hosted site (GitHub Pages) with an executable-docs
  suite, a cookbook of operator recipes, the generated coverage matrix,
  the cobra comparison gallery, and the *Inside the DSL* page; offline
  wheelhouse (niwaki + all dependencies) attached to every GitHub
  Release for restricted networks.
- The full unit-test suite (14,200+) ships with the repository and runs
  in the public CI on Python 3.12 and 3.13.

### Changed

- The generated cursor layout scales: one module per design domain,
  loaded lazily; ancestor makers are inherited through per-position
  mixins (nearest level wins, like the runtime) — 25k generated lines
  became 4.3k at 57 positions, ~75 lines per position since.
- Cursor class names disambiguate with as many ancestor labels as
  needed (`bgpPeerP` under two positions yields distinct cursors).

## [0.3.0] — 2026-07-11

First PyPI release.

### Added

- Published on PyPI: `pip install niwaki` / `uv add niwaki` (trusted
  publishing with provenance attestations, from the public repository).
- Fabric ASN as a curated position (`bgp_instance().autonomous_system()`),
  per-port interface-profile convention support proven in the live
  walkthrough (one selector per port, reserved ports never profiled).

### Fixed

- `plan` reads are scoped with `rsp-subtree-class` to the classes the design
  declares — an unscoped full read of `uni/fabric` exceeds the APIC query
  limit ("result dataset is too big").
- Field comparison in `plan` is numeric-aware: the APIC canonicalises
  numeric strings ("80.0" reads back as "80.000000"); designs carrying
  float-like values stay idempotent.
- `fvSubnet.scope` carries its operator name (was
  `visibility_of_the_subnet`).

## [0.2.0] — 2026-07-11

### Changed

- **The SDK is now named `niwaki`** (庭木 — the Japanese art of sculpting
  full-size, living garden trees).  The former working name collided with an
  existing PyPI package; `niwaki` is free as both a distribution and an
  import name, and says exactly what the SDK does to the APIC Management
  Information Tree.
- Everything follows the new name: the import package (`import niwaki`),
  the clients (`Niwaki`/`AsyncNiwaki`), the nodes
  (`NiwakiNode`/`AsyncNiwakiNode`) and the exception root (`NiwakiError`).
  No behavioural change.

## [0.1.0] — 2026-07-10

Initial private milestone, under the project's former working name.

- Design-first architecture: the design DSL describes the whole
  `uni` subtree (tenant, access, fabric, controller), `push()` applies
  (`strict` / `staged` / `plan`), the facade observes (navigation, typed
  reads, queries, delete).
- 2,222 generated Pydantic models (APIC v6.0 schemas), 558 enums,
  human-readable field names with wire aliases.
- Curated vocabulary (`domain/vocabulary.yaml`), typed cursors generated per
  position, unified reference resolver (name + DN flavors, abstract targets),
  `bind_dn()` escape, atomic staged classes.
- Sync + async transport with proactive token refresh, retry, pagination.
- Sphinx documentation with a generated vocabulary book; 13,700+ unit tests,
  mypy strict.
