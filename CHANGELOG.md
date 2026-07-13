# Changelog

All notable changes to this project are documented here.  The format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow semver
(0.x — the API may still change between minor versions).

## [Unreleased]

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
