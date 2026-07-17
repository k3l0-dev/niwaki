# 02 — Fabric access policies

Access policies under `uni/infra`: the switch/interface policy shelf an operator
builds before wiring up a leaf or spine. The suite provisions encapsulation
pools, domains and the attachable entity profile, switch profiles, and then the
full set of **interface-level policies** a leaf/spine access policy group binds —
CDP, LLDP, MCP, link-level, LACP (bundle + member), link-flap, L2, STP, storm
control, PoE, Fibre-Channel, MACsec, SyncE, 802.1X, per-interface CoPP, the QoS
flow-control trio (LLFC/PFC/slow-drain), and NetFlow (record/exporter/monitor).

```bash
uv run pytest tests/integration/02_fabric-access -m integration -s
```

See [`../README.md`](../README.md) for what these walkthroughs are — and are not.
The values here are illustrative; the job is to prove the SDK can express every
config combination the access domain offers, and that the APIC accepts it.

## Stats

| Metric | Value |
| --- | --- |
| Files | 16 (`test_001`–`test_016`) |
| Test functions | 45 |
| Interface-policy files (`test_004`–`test_016`) | 13 files, 37 test functions |
| Config objects declared (interface-policy files) | ~1 247 managed objects |
| Live result | 45/45 passed on the 6.0(9c) simulator |
| Faults after push | 0 (independent read-back per class, `rsp-subtree-include=faults`) |

The three foundational files (`test_001` switch profiles, `test_002` encapsulation
pools, `test_003` domains + AAEP) lay the groundwork; the interface-policy files
carry the bulk of the combination coverage.

## What the suite covers

The interface-policy files sweep the combination space rather than posting one
object per policy. Each object also carries the universal children (`tagTag`,
`tagAnnotation`, `aaaRbacAnnotation`). The axes per major object:

| File | Object | Combination axes |
| --- | --- | --- |
| `004` | `cdpIfPol` | both admin states |
| `004` | `lldpIfPol` | receive × transmit × DCBX version (cartesian, 8) |
| `004` | `mcpIfPol` | admin × mode × per-VLAN-PDU (cartesian, 8) |
| `005` | `fabricHIfPol` | every speed (12), every FEC mode (8), every auto-negotiation mode, both media types, both EMI-retrain values |
| `006` | `lacpLagPol` | every LACP mode (6) + control-flag combinations + min/max links; each carries an `l2LoadBalancePol` hash-field set |
| `006` | `lacpIfPol` | both transmit rates |
| `007` | `l2IfPol` | QinQ × VEPA × VLAN-scope (cartesian, 16) |
| `007` | `stpIfPol` | control-flag combinations (unspecified / guard / filter / both) |
| `008` | `stormctrlIfPol` | percentage-rate (action × packet-type, 8), packets-per-second (per-type rate/burst, both actions), and per-traffic-type percentage (broadcast / multicast / unk-ucast, each on its own policy, both actions) |
| `009` | `poeIfPol` | power-mode × policing-action × port-priority (cartesian, 18) + admin states + named max-power budgets |
| `009` | `poeInstPol` | power-control combinations |
| `010` | `fcIfPol` | port-mode × trunking (cartesian, 8) + every speed / auto-max-speed / fill-pattern |
| `010` | `fcInstPol` | control-flag combinations |
| `011` | `macsecParamPol` | cipher-suite × confidentiality-offset × security-policy (cartesian, 24) |
| `011` | `macsecKeyChainPol` / `macsecIfPol` | keychains with multiple keys; interface policy across both admin states, binding a keychain |
| `012` | `synceEthIfPol` | admin × QL-option-type (with selection/SSM booleans swept both ways); the three mutually exclusive QL-specification modes (exact / low-high / high-only) each on their own policy per option family |
| `012` | `synceInstPol` | admin × node-QL-option × transmit-DNU (cartesian, 12) |
| `013` | `l2PortAuthPol` | admin × host-mode (cartesian, 8) + config child (MAC-auth / re-auth) |
| `014` | `qosLlfcIfPol` | receive × transmit (cartesian, 4) |
| `014` | `qosPfcIfPol` | every mode |
| `014` | `qosSdIfPol` | congestion-clear-action × flush-admin (cartesian, 6) |
| `015` | `coppIfPol` | one `coppProtoClassP` per protocol (9) + non-overlapping multi-protocol classes + one policy whose single class matches all protocols |
| `016` | `netflowRecordPol` | collect/match parameter combinations (every collect flag, every match flag) |
| `016` | `netflowExporterPol` | source-IP-type sweep + a per-DSCP-value sweep (every DSCP on its own v9 exporter), each binding a reference VRF and EPG |
| `016` | `netflowMonitorPol` | binding an exporter and a record |

## APIC combination constraints discovered live

Values the model accepts but the controller rejects — the constraints the schema
does not encode. Each is worked around in the suite so the push stays clean:

1. **LACP `load-defer` control flag** — a valid `PcIfControl` value in the schema,
   but the controller answers *"Unable to configure load-defer … not a supported
   option"*. Left out of the control-flag combinations.
2. **MACsec keychain start times must be unique** — two keys in one keychain
   cannot share a `startTime` (*"All Start Times in a Keychain must be unique"*).
   The multi-key keychain gives its second key a distinct start timestamp.
3. **SyncE QL value is mandatory once a QL option is set** — the schema treats the
   quality-level values as optional, but the moment `qloptype` is anything other
   than `none` the controller demands one (*"QL option type is configured, but no
   QL value is specified"*). Each non-`none` option supplies a matching
   quality-level value from its family (`op1` → `fsync-ql-o1-*`, etc.).
4. **SyncE QL modes are mutually exclusive** — the controller accepts exactly one
   of `(exact)`, `(low-high)`, or `(high)` on a policy (*"Please specify either
   (exact) OR (low-high) OR (high) QL value"*). Rather than dropping the extra
   modes, they are **factored** into separate policies per option family, so all
   three are exercised.
5. **CoPP protocol may appear in only one class per policy** — a protocol cannot be
   named by two `coppProtoClassP` classes within the same `coppIfPol`
   (*"Proto already specified"*). The multi-protocol classes partition the
   protocol set; the all-protocol match is **factored** into its own policy as a
   single class.
6. **NetFlow export version** — the simulator accepts only version 9
   (*"Only Version 9 supported"*), though `cisco-v1` and `v5` are valid schema
   enum values. The version sweep collapses to `v9` on this platform.
7. **NetFlow custom source IP must be a subnet, mask ≤ /20** — a custom exporter
   source address is rejected as a host address; it must be a subnet whose mask is
   no longer than `/20` (*"can only have a mask up to /20"*). The suite uses
   `192.0.0.0/20`.
8. **Storm-control per-type rates are silently reset unless the config-valid flag
   is set** — the broadcast / multicast / unknown-unicast percentage rates are
   accepted by the POST but reset to their defaults on read unless
   `isUcMcBcStormPktCfgValid` is `Valid`. No error is returned; the per-type
   policies set the flag so the rates actually apply (confirmed by read-back).

## Skipped / abandoned and why

**Factored, not skipped** — mutually exclusive settings that cannot coexist on one
object are split so both sides are still covered:

- SyncE QL specification modes (exact / low-high / high-only) — one policy each
  (constraint 4).
- CoPP all-protocol match — a dedicated policy with a single all-protocol class,
  alongside the per-protocol and partitioned-combo policies (constraint 5).
- Storm-control per-traffic-type percentage rates — broadcast / multicast /
  unknown-unicast each on their own policy.
- NetFlow DSCP — swept on separate v9 exporters (version is platform-pinned, so
  DSCP is factored across instances rather than cartesian'd with version).

**Controller / platform unsupported** (schema-valid, rejected live — genuinely
uncoverable on this controller, encoded as a platform note, not swept):

- LACP `load-defer` control flag (constraint 1) — rejected as "not a supported
  option" in any object, so it cannot be factored.
- NetFlow export versions `cisco-v1` and `v5` — the 6.0(9c) simulator supports
  only `v9` (constraint 6). Other versions may work on physical hardware.

**Coverage gaps** — relations that exist in the object model but are not reachable
through a maker, bind, or verb in the DSL, so they are reported rather than forced:

- `poeRsPoeEpg` on `poeIfPol` — the PoE interface policy's relation to the EPG of
  the powered device has no bind alias.
- `macsecRsToParamPol` on `macsecIfPol` — the interface policy's relation to its
  access-parameters policy is not exposed; only the keychain relation is.

**Managed-tag children** (`tagExtMngdInst`, `tagInst`) — reachable only through the
low-level `.mo()` escape, and configuring them flips the parent object to
`extMngdBy=msc` and spawns shadow annotations, so they are deliberately left out.
The three ordinary universal children (`tagTag`, `tagAnnotation`,
`aaaRbacAnnotation`) are applied everywhere.

**Out of this domain** (reachable elsewhere, not access-policy objects):

- `l2InstPol` (leaf-global L2/MTU policy) — a fabric-domain object, covered under
  `03_fabric`.
- `l2PortSecurityPol` at the infra level — the port-security policy is curated
  only under a tenant static path.
- `xcvrOpticsIfPol` — an abstract/derived class (no relative name, empty
  containment); a bind target only, never a standalone creatable policy.
- MST (`stpInstPol` and its region/domain subtree) — the MST instance policy is a
  non-creatable singleton, so touching it would mean configuring the fabric
  default. Deliberately left alone (a named object is created for every other
  policy; defaults are only ever referenced, never reconfigured).
