# 03 — Fabric-wide policies

Fabric policies under `uni/fabric`: switch/interface/protocol policies, monitoring
destinations, system settings, vPC protection, and fabric SPAN/TWAMP. Each file
provisions the live sim through the SDK and leaves the objects in place for
inspection.

```bash
uv run pytest tests/integration/03_fabric -m integration -s
```

This is a **capability walkthrough**, not a production configuration: the values
are illustrative and chosen to exercise the widest possible slice of the SDK's
config surface — every maker, every enum value, both settings of every boolean,
several `Flags` combinations, and cartesian products over the fields that
interact. See [`../README.md`](../README.md) for what these walkthroughs are and
are not.

## Stats

| Metric | Value |
| --- | --- |
| Test files | 16 |
| Test functions | 42 |
| Config objects pushed (MOs) | 592 |
| Live result | 42/42 passed |
| Faults raised by the APIC | 0 |

Verified live against a Cisco APIC simulator running 6.0(9c).

## What the suite covers

The suite drives the `fabric()` design root exhaustively. The combination axes
per major object:

| File | Objects | Combination axes exercised |
| --- | --- | --- |
| `test_001_isis` | IS-IS domain policy + level | `(MTU, redistribution-metric)` pairs; level-1 fast-flood both ways with a full timer spread |
| `test_002_link_policies` | LLDP, link-level, link-flap, L3-if, L2-MTU, VXLAN, node-control | LLDP receive × transmit × DCBX-version (full 8-way); debounce / flap / MTU spreads; every named VXLAN UDP port plus numeric ports; node feature-selection × control bitmask |
| `test_003_macsec` | parameters, keychains, interface policies | cipher-suite (4) × security-policy (2); keychains carrying staggered keys; interface admin × auto-keys |
| `test_004_optics` | ZR-S, ZRP-S, DWDM | admin × DWDM carrier grid; oFEC/DAC pairing; a spread across the 96 DWDM channels |
| `test_005_singletons` | COOP, load-balance, WWN, BGP-RR, PSU | both COOP auth modes and every valid load-balance combination pushed in turn; all eight PSU redundancy modes; BGP route-reflector node endpoints data-driven from the spines |
| `test_006_callhome` | callhome + smart callhome | every urgency level × message format × admin/RFC flags; both admin states and both secure-SMTP settings across the two protocol profiles |
| `test_007_snmp_tacacs` | SNMP, TACACS destinations | SNMP version, and v3 security level (noauth/auth/priv); TACACS auth-protocol × command-argument logging |
| `test_008_syslog` | syslog groups + sinks | one group per message format; every severity × forwarding facility, cycling transport and admin; console / file / protocol-profile sinks |
| `test_009_datetime` | NTP policies + display format | `(admin, server, master, authentication)` combinations; both key types, trusted both ways; provider preferred / true-chimer flags; both display formats × offsets across a spread of time zones |
| `test_010_dns_geo` | DNS profiles, geo tree | IPv4 / IPv6 preference; preferred + non-preferred providers; default + non-default domains; full site→building→floor→room→(row→)rack nesting |
| `test_011_communication` | management-access services | HTTP redirect states; HTTPS TLS protocol-set combinations with cipher entries; SSH KEX/cipher/MAC `Flags` combinations; shell-in-a-box / setup / response-time / restart |
| `test_012_global_proxy` | global EP-listen, connectivity preference, proxy | both endpoint-listen states (enabled on a VLAN encapsulation, and disabled); both connectivity preferences; proxy with several ignore-host entries |
| `test_013_vpc` | vPC domain policies, explicit protection | a spread of peer-dead / delay-restore timers; all three pairing modes pushed in turn; explicit protection groups pairing discovered leaves |
| `test_014_span_sources` | SPAN source groups | admin state × mirror direction; VSPAN sources and abstract source definitions; a sweep of match-label colors |
| `test_015_span_destinations` | SPAN destination groups | local-port destinations; ERSPAN-to-EPG across both versions, a DSCP spread and both visibility modes |
| `test_016_vspan_twamp` | VSPAN sessions/destinations, TWAMP | session admin states; VSPAN direction; ERSPAN version/mode; TWAMP responder/server across admin states and timers |

External collectors (SNMP, syslog, NTP, DNS) are associated with the out-of-band
management EPG; the ERSPAN destinations reference the in-band management EPG. All
node and path references (BGP route reflectors, vPC pairs, SPAN sources) are
data-driven from the fabric at runtime.

## APIC combination constraints discovered live

These are combinations the controller rejects at push time that the object schema
does not flag. Rather than dropping one side of a mutually-exclusive constraint,
each exclusive setting is **factored** into its own object (or, for a singleton,
its own push) so both sides are exercised: an LFR load-balance policy *and* a
DLB-on one, a ZR-S `cFEC` optic *and* a ZRP-S `oFEC` optic, a UDP RFC-5424 syslog
group *and* other-transport groups, one SPAN session per destination, and so on.

- **Load balancing** — flowlet prioritization is accepted only with LB mode
  `traditional` and dynamic load balancing `off`; the three are mutually
  exclusive with an active DLB mode.
- **Global endpoint-listen** — the encapsulation must be a VLAN with a valid VLAN
  id; an empty encapsulation is rejected.
- **Fabric node control** — the `mixed` feature (NetFlow + Telemetry combined) is
  rejected in this release.
- **IS-IS** — only level-1 is supported; a level-2 component is rejected.
- **MACsec keychain** — all key start-times within a keychain must be unique.
- **ZRP-S optics** — the FEC/DAC pairing is pinned (`oFEC` requires DAC `1x1.25`,
  `cFEC` requires `1x1`); with `cFEC` the chromatic-dispersion range narrows to
  ±2400.
- **Object descriptions** — the `descr` field rejects the `=` character.
- **Syslog** — the enhanced RFC-5424 format accepts only the UDP transport.
- **HTTPS** — client-certificate authentication requires a CA trustpoint; TLSv1.3
  cannot be combined with TLSv1 / TLSv1.1.
- **SPAN / VSPAN** — the APIC is not accepted as a SPAN destination; a source or
  destination group session references exactly one destination (one match
  label); node-level SPAN requires span-on-drop and only one such session may
  exist per node; an ERSPAN-to-EPG destination requires the analyser IP and
  source-prefix on the destination relation.

## Skipped / abandoned and why

**Factored — both sides of a mutually-exclusive constraint are now covered:**

- Load balancing — a flowlet (LFR) policy, a traditional/prioritized policy and
  policies for each dynamic-load-balancing mode are pushed in turn.
- Coherent optics — `cFEC` on the ZR-S policies and `oFEC` on the ZRP-S policies.
- Optic FEC/DAC — the pinned `oFEC`/`1x1.25` pairing on ZRP-S.
- Syslog transport — a UDP RFC-5424 group alongside the TCP/SSL/UDP groups in the
  ACI and NX-OS formats.
- HTTPS TLS — a modern (TLSv1.2/1.3) set and a legacy (TLSv1/1.1/1.2) set.
- SPAN/VSPAN — one session per destination, and one destination group per
  destination (local-port groups and per-spec ERSPAN groups).
- Singleton alternate states, each pushed in turn: both COOP authentication modes,
  every accepted PSU redundancy mode (all eight, including `not-supp` / `unknown`),
  all three vPC pairing modes, both endpoint-listen states, both connectivity
  preferences, and the display-format / offset / timezone spread.

**Genuinely uncoverable (kept documented):**

- IS-IS level-2 and the `mixed` fabric node-control feature — rejected by the
  controller.
- ZRP-S `cFEC` — the coherent-optics chromatic-dispersion range it requires
  cannot be expressed (the generated model pins that field to a single value that
  is out of range for `cFEC`), so `cFEC` is exercised on the ZR-S policies only.
- Node-level SPAN (a source bound to a fabric node with span-on-drop) — the
  fabric permits one span-on-drop session per node and the node slots are already
  occupied; port-level SPAN is provisioned instead.
- HTTPS client-certificate authentication — requires a CA trustpoint that is not
  reachable via the DSL (see coverage gaps), so it is left disabled.

**Cross-domain (belongs to another phase, not this one):**

- SPAN source bindings to EPG / BD / VRF / L3Out / filter-group target tenant and
  access objects; they are exercised in the tenant and observability phases.
- The AES passphrase for configuration export/import encryption lives off the
  design root (`uni/exportcryptkey`), not under `uni/fabric`.

**Curated parent, child unreachable via the DSL (coverage gaps):**

- `macsecFabIfPol` — the relations linking a fabric MACsec interface policy to
  its parameter and keychain policies are not curated.
- `bgpInstPol` — the external and inter-site route-reflector profiles and the
  domain-id base are not curated (the intra-fabric route reflector and
  autonomous-system profile are).
- `tacacsTacacsDest` — no management-EPG association (SNMP, syslog, NTP and DNS
  destinations have one).
- `callhomeProf` — the SMTP relay server is not curated.
- `commHttps` — the key-ring and client-cert CA trustpoint are not curated;
  `commPol` — the telnet service is not curated.
- The universal metadata children (`tag`, `annotation`, RBAC/domain-tag
  references) are not part of the curated write surface; the scalar `annotation`
  field is available on every object.
