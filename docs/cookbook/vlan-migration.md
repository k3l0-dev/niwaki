# VLAN migration — a day-2 change with a safety net

**Problem** — the `web` workload must move from VLAN 120 to VLAN 220.  That
touches two places: the pool must cover the new VLAN, and the static path
must re-encap.  A day-2 change is a *smaller design* — declare only what
changes, plan it, push it.

## Starting point

The state from the earlier recipes — pool, domain, EPG and its static path
on VLAN 120:

```python
from niwaki import Niwaki
from niwaki.design import design, tenant

aci = Niwaki.connect("https://apic.example.com", "admin", "secret")

base = design()
base.infra().vlan_pool("prod-static", "static").range("vlan-100", "vlan-199")
web = base.tenant("shop").app("storefront").epg("web")
web.static_path("topology/pod-1/paths-101/pathep-[eth1/13]", encap="vlan-120")
base.push(aci)
```

## The migration design

Only the delta is declared: a second range extends the pool, and the same
path DN carries the new encap:

```python
migration = design()
migration.infra().vlan_pool("prod-static", "static").range("vlan-200", "vlan-299")

web = migration.tenant("shop").app("storefront").epg("web")
web.static_path("topology/pod-1/paths-101/pathep-[eth1/13]", encap="vlan-220")
```

Parents (`infra`, the pool, the tenant chain) are declared without
attributes: they travel as upserts that touch nothing.

## Plan — the safety net

```python
plan = migration.push(aci, mode="plan")

assert "uni/infra/vlanns-[prod-static]-static/from-[vlan-200]-to-[vlan-299]" in plan.creates
(path_dn,) = list(plan.updates)
assert plan.updates[path_dn] == {"encap": ("vlan-120", "vlan-220")}
```

The plan says precisely: *one new range, one field changing from `vlan-120`
to `vlan-220`, nothing else*.  This is the review artifact — in a pipeline,
this is what lands in the merge request ({doc}`gitops-pipeline`).

## Push, verify, converge

```python
migration.push(aci)

paths = aci.query("fvRsPathAtt").fetch()
assert [p.encap for p in paths] == ["vlan-220"]

assert migration.push(aci, mode="plan").has_changes is False
```

## Variations & pitfalls

- **The old range stays** — a design never deletes what it does not
  declare, so `vlan-100`–`vlan-199` survives until you retire it
  explicitly (`aci.node(...).delete()`), *after* the last consumer moved.
- **Re-encap is disruptive per path** — the APIC reprograms the port; for
  a fleet, migrate in waves (the inventory loop is plain Python) and let
  `plan` gate each wave.
- **Watch the change land** — `aci.query("fvRsPathAtt").where(encap="vlan-220")`
  confirms programming intent; the operational truth per port lives in the
  `fv` deployment classes ({doc}`fabric-audit`).
