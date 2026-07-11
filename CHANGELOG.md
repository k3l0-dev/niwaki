# Changelog

All notable changes to this project are documented here.  The format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow semver
(0.x — the API may still change between minor versions).

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
