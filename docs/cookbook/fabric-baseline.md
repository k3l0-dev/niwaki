# Fabric baseline — NTP, DNS, syslog, BGP route reflectors

**Problem** — a fresh fabric needs its day-0 plumbing: time sources, name
resolution, log shipping, and the BGP route reflectors that every ACI pod
requires.  These live under `uni/fabric` and follow the exact same design
mechanics as tenants.

## The design

```python
from niwaki import Niwaki
from niwaki.design import fabric

config = fabric()

ntp = config.datetime_policy("prod-time")
ntp.ntp_provider("10.0.0.1", preferred_state=True)
ntp.ntp_provider("10.0.0.2")

dns = config.dns_profile("default")
dns.provider("10.0.0.53")
dns.domain("example.com")

config.syslog_group("central-syslog").remote_destination(
    "10.0.0.99", severity="warnings"
)

bgp = config.bgp_instance("default")
bgp.autonomous_system(autonomous_system_number="65001")
reflectors = bgp.route_reflector()
reflectors.node("101")
reflectors.node("102")
```

Positions without a name in ACI (`bgpAsP` is always `as`, `bgpRRP` always
`rr`) take no name argument — the DSL mirrors the schema, it does not
invent one.

## Plan, push

```python
aci = Niwaki.connect("https://apic.example.com", "admin", "secret")

plan = config.push(aci, mode="plan")
print(f"{len(plan.creates)} fabric objects to create")

config.push(aci)
```

## Verify

```python
providers = aci.query("datetimeNtpProv").fetch()
assert {p.name for p in providers} == {"10.0.0.1", "10.0.0.2"}

reflector_nodes = aci.query("bgpRRNodePEp").fetch()
assert sorted(n.node_id for n in reflector_nodes) == ["101", "102"]

assert config.push(aci, mode="plan").has_changes is False
```

## Variations & pitfalls

- **Route reflectors are not optional** — without `bgpRRNodePEp` entries the
  fabric's MP-BGP never comes up and L3Outs stay dark.  Pick two spines.
- **The `default` names matter** — the fabric consumes `dns_profile` and
  `bgp_instance` through policy groups that reference them by name;
  `default` is what the out-of-box selectors point at.  Renaming means also
  re-pointing the fabric policy group (outside the curated vocabulary —
  `.mo()` territory).
- **Day-2 is one field** — flipping the preferred NTP server later is a
  two-line design:
  `fabric().datetime_policy("prod-time").ntp_provider("10.0.0.2", preferred_state=True).push(aci)`.
- **Severity vocabulary** — syslog severities are the ACI enum
  (`emergencies`…`debugging`); a wrong value fails at the call site, not on
  the APIC.
