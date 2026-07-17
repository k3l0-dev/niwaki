# 08 — Observability

Live walkthroughs that provision the fabric's **observability plane** through the
niwaki SDK — monitoring policies, SPAN/VSPAN, NetFlow and syslog — and let the
APIC accept or reject what the SDK produces. Each file reads like a runbook: build
a design, push it, observe the result.

```bash
uv run pytest tests/integration/08_observability -m integration -s
```

Unlike a single "one object per feature" smoke test, this phase drives the config
surface **exhaustively** — every enum value, both boolean states, several flag
combinations, and a cartesian over the fields that interact — to prove the SDK can
express each knob the controller exposes. See [`../README.md`](../README.md) for
what these walkthroughs are, and what they are not: the addresses, severities and
names are **illustrative**, chosen to exercise the SDK, not to model a production
monitoring baseline.

## At a glance

| Metric | Value |
| --- | --- |
| Files | 11 |
| Test functions | 27 |
| Config objects pushed (managed objects) | 882 |
| Live result | **27 / 27 accepted** |
| Configuration faults | **0** |

| File | Tests | MOs | Focus |
| --- | --- | --- | --- |
| `test_001_monitoring_tenant.py` | 3 | 108 | Tenant EPG monitoring: source per severity, targets per scope, severity × include-action |
| `test_002_monitoring_infra.py` | 2 | 45 | Access monitoring policy: fault/event severity cartesian |
| `test_003_monitoring_fabric.py` | 3 | 73 | Fabric + common monitoring policies, switch health retention |
| `test_004_span_access.py` | 4 | 64 | Access SPAN: filter entries, path/L3Out sources, ERSPAN destinations |
| `test_005_span_tenant.py` | 3 | 131 | Tenant SPAN: EPG sources × direction, across several tenants |
| `test_006_span_fabric.py` | 3 | 71 | Fabric SPAN: node span, BD/VRF sources, ERSPAN destinations |
| `test_007_netflow_access.py` | 3 | 44 | Access NetFlow: record/exporter/monitor/node/VMM |
| `test_008_netflow_tenant.py` | 1 | 108 | Tenant NetFlow, factored across several tenants |
| `test_009_syslog_groups.py` | 1 | 33 | Syslog groups: format × timestamp, console/file/profile sinks |
| `test_010_syslog_remotes.py` | 2 | 38 | Remote syslog: format × transport, facility × severity, over OOB |
| `test_011_erspan_matrix.py` | 2 | 167 | ERSPAN destination matrix: DSCP × mode, TTL sweep |

Every external collector or destination (syslog remote, NetFlow exporter over
management) is reached through the out-of-band management EPG.

## What the suite covers

### Monitoring policies

The four monitoring scopes — tenant EPG (`monEPGPol`), access (`monInfraPol`),
fabric (`monFabricPol`) and the fabric-wide common (`monCommonPol`) — each carry
the full notification-source set and their lifecycle/limit policies:

- **Sources** — one syslog source per syslog severity (8), one SNMP source per
  condition severity (6), one callhome source per urgency (8), one TACACS source
  per condition severity (both switch-audit states) and a smart-callhome source.
  A source carries one severity and one include-action flag set, so their cross is
  factored out onto its own policy — one syslog source per (severity, include-action)
  pair.
- **Severity assignment** — fault-severity policies sweep the ordered
  `(initial, target)` pairs plus the `inherit` special; event-severity policies
  cover every accepted initial value.
- **Targets** — monitoring targets over a spread of target-scope classes.
- **Lifecycle / limits** — the generic fault-lifecycle policy and the statistics
  instance-limit policy; a switch health-score retention policy over a
  size × purge-window grid.

### SPAN and VSPAN

SPAN is exercised at every scope, with each relation placed where the controller
allows it:

- **Access (infra) SPAN** — a filter group with one entry per IP protocol
  (plus L4 port ranges), sources spanning an interface **path** in every direction
  and an **L3Out**, the virtual-source makers, and ERSPAN destinations sweeping the
  visible/not-visible mode, TTL, DSCP and MTU space.
- **Tenant SPAN** — sources spanning each of several **EPGs** in every direction,
  ERSPAN destination groups, and a VSPAN session/destination group. The source
  surface is factored across **several tenants** to show namespace independence.
- **Fabric SPAN** — a **node-span** source per fabric node, **bridge-domain** and
  **VRF** sources over several BDs/VRFs in every direction, and ERSPAN destinations.
- **ERSPAN matrix** — an ERSPAN summary carries one mode, one DSCP mark and one
  TTL, and a destination group holds one destination, so the value space is factored
  into one destination group per combination: every **DSCP mark under each
  visibility mode**, and a full **TTL sweep**.

### NetFlow

- **Records** — one record per collect-parameter flag and one per match-parameter
  flag, plus multi-flag combinations (IPv4 5-tuple, L2 key).
- **Exporters** — one per source-IP type (custom, in-band, out-of-band, PTEP) over
  a spread of DSCP marks; tenant exporters additionally bind the VRF and EPG behind
  which the collector resides. Every exporter pins NetFlow version 9.
- **Monitors** — tying records to one or two exporters.
- **Node policies** over the MTU space, and a VMM exporter.

### Syslog

- **Groups** — a spread of destination groups sweeping the format
  (`aci`/`nxos`/`rfc5424-ts`) and timestamp options, each carrying a console sink,
  a file sink and a protocol profile so every console/file severity, every format
  and both admin states are exercised.
- **Remote destinations** — a remote destination carries one format and one
  transport, so the **format × transport** cross is factored into one collector per
  pair; a second group sweeps every forwarding facility (`local0`..`local7`) and
  every syslog severity. Every collector is reachable over the OOB management EPG.

## APIC combination constraints

These are enforced by the controller at push time, not by the object schema — the
schema accepts the values, the fabric rejects the combination. The suite encodes
each one so the pushes stay clean:

- **Monitoring lifecycle** — a fault-lifecycle policy under *any* monitoring policy
  must be the **generic policy (code 0)**; a named/coded lifecycle child is rejected
  on all four monitoring classes.
- **Fault severity** — the initial severity must be **warning or higher**, and the
  target severity must be **equal to or higher than** the initial.
- **Event severity** — the initial severity **cannot be cleared**.
- **SPAN relation scope** — the **EPG** SPAN-source relation is valid only for
  tenant SPAN; the **L3Out** relation only for access (infra) SPAN; the
  **bridge-domain / VRF** relations and **node span** only for fabric SPAN.
- **Node span** requires **span-on-drop**; span-on-drop is not accepted under
  access SPAN.
- **SPAN destinations are ERSPAN** — a SPAN destination carries a **destination IP**;
  the EPG destination relation is rejected (the controller demands the IP even when
  an ERSPAN summary is present). An **APIC** SPAN destination is not supported.
- **One destination per session** — a SPAN destination group holds a single
  destination, so each ERSPAN target lives in its own group.
- **NetFlow version** — only **version 9** is accepted.
- **NetFlow source address** — a custom-source exporter takes a **unicast** address
  with a mask of **/20 or shorter** (not a network/zero-host address); the VMM
  exporter takes a **bare host** address.
- **Syslog timestamp** — the timezone and millisecond timestamp options are accepted
  **only with the ACI format**.

## Factored, not skipped

Where two settings cannot coexist on a single object — because the controller
scopes a relation to one SPAN type, or because a field holds a single value — the
suite puts **each side in its own object** and covers both, rather than dropping
the combination:

- **SPAN relation scope** — the EPG, L3Out and bridge-domain/VRF/node relations are
  each valid at only one SPAN scope, so each is exercised in its own SPAN type
  (tenant / access / fabric).
- **ERSPAN mode / DSCP / TTL** — one object holds one of each and one destination
  per group, so every combination gets its own destination group.
- **Syslog format × transport** and **monitoring severity × include-action** — one
  object per pair.
- **Tenant SPAN / NetFlow** — the same surface is replayed across several tenants.

## Genuinely uncovered, and why

These have no valid live target on this fabric and are left out rather than forced:

- **Notification source → destination group** — a syslog / SNMP / callhome / TACACS
  source under a monitoring policy has no binding to its destination group in the
  SDK vocabulary, so the sources carry their severity and include-action attributes
  only.
- **EPG SPAN destination** — the EPG destination relation for SPAN and VSPAN is
  rejected by the controller (an ERSPAN destination IP is always required, and the
  relation is refused even alongside an ERSPAN summary), so every destination is
  ERSPAN.
- **Extended SPAN filter entry** and the virtual-source target relations
  (`spanVSrc` / `spanVSrcDef`) are not in the vocabulary; the objects carry their
  attributes only.
- **NetFlow exporter management EPG** — a NetFlow exporter has no management-EPG
  binding; out-of-band reachability is expressed through the exporter's
  `oob-mgmt-ip` source-IP type instead.
- **Stats-collection and health children** of the monitoring policies (hierarchical
  stats collection, statistics/health-score policies, export policies) are not in
  the vocabulary.
- **Universal metadata children** (tags, annotations, RBAC/domain-tag references)
  have no maker across the vocabulary; the scalar `annotation` field is set instead.

Each source file repeats the gaps that apply to it in a `COVERAGE GAPS` header.
