# Changelog

All notable changes to this project are documented here.  The format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow semver
(0.x — the API may still change between minor versions).

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
