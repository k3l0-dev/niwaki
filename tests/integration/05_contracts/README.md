# 05 — Tenant contracts

Tenant security policy, provisioned through the SDK and pushed to a live APIC:
filters and their entries, contracts with subjects and the six label kinds, taboo
contracts, `vzAny`, imported and out-of-band contracts, service-graph attachments,
and QoS requirements with data-plane policing.

```bash
uv run pytest tests/integration/05_contracts -m integration -s
```

These are **exhaustive, non-production** walkthroughs: the goal is to prove the SDK
can express every configuration combination the domain offers, so the values are
illustrative and the object counts are deliberately high. See
[`../README.md`](../README.md) for what these walkthroughs are — and are not.

## Stats

| Metric | Value |
| --- | --- |
| Files | 12 (`test_001_*` … `test_012_*`) |
| Test functions | 33 |
| Config objects pushed | ~960 |
| Live result | 33/33 pass |
| Faults raised | 0 |

Every file owns a manual `wipe(aci)` that removes only the top-level DNs it created
(a tenant/VRF/contract delete cascades its children). `wipe()` is never called by the
suite — it is run by hand via `tests/integration/wipe.py 05_contracts`.

## What the suite covers

The suite sweeps each enum value at least once, both values of every bool, several
`Flags` combinations, and takes a cartesian across the fields that interact. Where a
combination cannot coexist on one object, both sides are **factored** onto separate
objects so each side is still exercised (see the next section).

| File | Object(s) | Combination axes |
| --- | --- | --- |
| `test_001_filters_l4` | `vzFilter` / `vzEntry` | TCP & UDP over named ports, numeric ports, tuple ranges, dashed-string ranges and explicit source+destination windows; every TCP session-flag value and representative multi-flag sets; `stateful` both ways; port-less all-fragment entries |
| `test_002_filters_icmp` | `vzEntry` | every named ICMPv4 type (6) and ICMPv6 type (8); `apply_to_frag` both ways |
| `test_003_filters_ethertype` | `vzEntry` | every ether-type; every ARP opcode; every non-L4 IP protocol (EGP/EIGRP/IGMP/IGP/L2TP/OSPF/PIM); all 23 DSCP match values |
| `test_004_filters_portzero` | `vzEntryPortZero` | direction × ether-type × protocol cartesian; the all-fragments and stateful bits; a rotating DSCP match; every TCP flag combo on the TCP rows |
| `test_005_contracts` | `vzBrCP` | scope × QoS-class cartesian (4 × 7); every contract-level DSCP value (23); the `intent` values accepted at create time |
| `test_006_subjects` | `vzSubj`, `vzInTerm`/`vzOutTerm`, `vzRsSubjFiltAtt` | provider-match × consumer-match cartesian; apply-both-ways vs one-way with terminals; filter-binding action × directives × priority-override cartesian via `ref()`; every subject-level and terminal-level DSCP value (23 each) |
| `test_007_labels_exceptions` | six label classes, `vzException` | all six label kinds on a `vzAny`, cycling every one of the 140 policy colours, complement both ways where supported; exceptions across every match field (Ctx/Dn/EPg/Tag/Tenant) at contract and subject level |
| `test_008_taboo` | `vzTaboo`, `vzTSubj`, `vzRsDenyRule` | deny rules across every directives combination; a subject denying several filters at once |
| `test_009_vzany` | `vzAny` | match-type × preferred-group-member cartesian (one VRF per pair); provide/consume verbs; imported-contract reference |
| `test_010_imported_oob` | `vzCPIf`, `vzOOBBrCP` | imports reached closed-world (`bind`) and by raw DN (`bind_dn`); out-of-band contracts per scope plus a rich one with both-ways/one-way subjects, labels, terminals and exceptions |
| `test_011_qos` | `qosRequirement`, `qosDppPol`, `qosEpDscpMarking` | policer type × metering mode × sharing cartesian; every rate and burst unit; every conform/exceed/violate action (with the DSCP/CoS mark fields where marking); requirements that mark DSCP and wire ingress/egress policers |
| `test_012_service_graph` | `vnsAbsGraph`, `vzRsGraphAtt`, `vzRsSubjGraphAtt` | contract-level vs subject-level graph attachment factored across separate contracts; abstract-graph attributes (`filter_between_nodes` both ways, `svc_rule_type` across its values) |

## Factored constraints (mutually-exclusive combinations, both sides covered)

The APIC refuses some field combinations that are individually valid. Rather than
skip a value, the suite puts each side of the conflict on its **own** object so both
are exercised and every push is accepted:

- **`apply_to_frag` vs L4 port** — an entry applied to all IP fragments has no L4
  header, so it cannot carry a port. Covered both ways: the port-bearing L4 entries
  (`apply_to_frag=False`) in `test_001`, and the port-less all-fragment TCP/UDP/IP
  entries in the same file.
- **Wildcard vs specific ether-type** — an `unspecified` (match-anything) entry cannot
  share a filter with specific-ether-type entries. Covered both ways: the specific
  ether-types in one filter and the wildcard in its own filter (`test_003`).
- **TCP `est` vs other flags** — the *established* session flag cannot be combined with
  other TCP flags. Covered both ways: `est` on its own entry and multi-flag sets
  (`ack,syn`, `ack,fin,rst`, …) on other entries (`test_001` / `test_004`).
- **Contract-level vs subject-level service-graph attachment** — one contract cannot
  carry the graph attachment at both levels. Factored across separate contracts: one
  holds the contract-level `vzRsGraphAtt`, others the subject-level `vzRsSubjGraphAtt`
  (`test_012`).

## Non-factorable rules encoded (not a coexistence conflict)

These are single-field rules the suite simply respects so every object is valid:

- **Port ranges cannot start at 0** — `dFromPort = 0` with a non-zero `dToPort` is
  refused; wide ranges start at 1 (`unspecified` expresses "any port").
- **`descr` character set** — the APIC description pattern rejects `=`, `[` and `]`
  (among others), so descriptions use plain punctuation.
- **DSCP field types differ** — the filter/contract/subject/terminal DSCP fields take
  named values (`AF11`, `CS6`, `EF`, …), whereas the DPP policer mark fields take a
  plain integer.
- **Mark actions need mark fields** — a DPP conform/exceed/violate action of `mark`
  must carry its DSCP/CoS mark value; `drop`/`transmit` carry none.

## Genuinely uncoverable, and why

- **`vzRsFiltGraphAtt` on `vzFilter`** (filter-level graph attachment) — present in the
  object model but has no binding in the DSL vocabulary; left uncovered rather than
  reached by a raw-DN escape.
- **Terminal-level service-graph attachments** (`vzRsInTermGraphAtt` /
  `vzRsOutTermGraphAtt`) — the terminal `service_graph` binding resolves to the parent
  subject's attachment, so the terminal-level attachments cannot be declared
  independently. Contract-level and subject-level attachments are covered (`test_012`).
- **`intent=estimate_delete`** — this is an estimation *action*, not desired
  configuration: the APIC refuses it at create time (a contract must already be
  installed), and once set it pins the contract so it can no longer be deleted, which
  would break teardown. The two intents that are stable desired state — `install` and
  `estimate_add` — are covered.
- **SD-WAN and multi-site references** (`vzRsSdwanPol` on subjects; `mdpService` /
  `mdpRemoteService` on contracts) — out of scope for a single-fabric walkthrough:
  SD-WAN policy is refused by this APIC, and the multi-domain services belong to a
  multi-site/orchestrated deployment.

Service graphs, EPG/ESG contract wiring, and management EPGs have their own phases
(07 services, 04 tenant, 09 management). This phase provisions the pure contract
surface; `vzAny` is the in-phase home for provide/consume and the label kinds, and the
service-graph attachments here bind graph shells whose nodes are built in phase 07.
