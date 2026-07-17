# Micro-segmentation with ESGs

**Problem** — the storefront works, but its security still rides on EPGs, which
are welded to bridge domains and subnets.  Security now needs to follow the
*workload*, not the wire: every endpoint tagged `tier=web` belongs to the web
security zone wherever it lives, and the zones talk to each other over
contracts.  That is an **endpoint security group** (ESG) — classification
decoupled from the network, scoped to a VRF.

This continues the `commerce` deployment from {doc}`application-contracts`.

## The design

An ESG lives in a VRF and pulls endpoints in by **selector**.  Here each zone
selects on a policy tag, so onboarding a workload is a matter of tagging it —
no BD or subnet change.  The same contracts govern traffic between the zones,
provided and consumed on the ESGs:

```python
from niwaki import Niwaki
from niwaki.design import tenant

config = tenant("commerce")
config.vrf("prod")

app = config.app("storefront")

web = app.esg("esg-web").bind(vrf="prod")
web.tag_selector("tier", "web")
web.consume("web-to-app")

svc = app.esg("esg-app").bind(vrf="prod")
svc.tag_selector("tier", "app")
svc.provide("web-to-app")
svc.consume("app-to-db")

db = app.esg("esg-db").bind(vrf="prod")
db.tag_selector("tier", "db")
db.provide("app-to-db")

config.filter("f-http").entry("http", tcp=8080)
config.filter("f-postgres").entry("pg", tcp=5432)
config.contract("web-to-app").set(scope="vrf").subject("http").bind(filter="f-http")
config.contract("app-to-db").set(scope="vrf").subject("sql").bind(filter="f-postgres")
```

The `bind(vrf="prod")` is mandatory — an ESG has no meaning without a VRF scope —
and it sits on the ESG cursor, before the selectors and verbs that are its
children.  `tag_selector(key, value)` matches the policy tag; `provide` and
`consume` attach contracts exactly as they do on an EPG.

## Plan and push

```python
aci = Niwaki.connect("https://apic.example.com", "admin", "secret")

plan = config.push(aci, mode="plan")
print(f"{len(plan.creates)} objects to create")
config.push(aci)
```

The `with` form closes the session for you; `connect()` is used here so the
rest of the page can share one client — see {doc}`../guide/connection`.

## Verify

```python
esgs = aci.tenant("commerce").query("fvESg").fetch()
assert {e.name for e in esgs} == {"esg-web", "esg-app", "esg-db"}

selectors = aci.tenant("commerce").query("fvTagSelector").fetch()
assert {s.match_value for s in selectors} == {"web", "app", "db"}

assert config.push(aci, mode="plan").has_changes is False
```

## Variations & pitfalls

- **Selectors are additive** — an ESG can combine `tag_selector`,
  `ep_selector` (an IP or MAC match expression like
  `ep_selector("ip=='10.30.10.0/24'")`) and `epg_selector` (pull in a whole
  EPG by DN).  A workload matched by any selector joins the zone.
- **Tags are unique per VRF** — the same `key=value` tag classifies one way in a
  VRF; do not reuse a tag pair for two zones in the same VRF.
- **ESG contracts, not taboo** — ESGs use `provide` / `consume` / `intra_epg`;
  they do not support taboo contracts (the APIC rejects the attachment).  Model
  deny-intent with a tighter contract set instead.
- **Migrating off EPGs** — you can run EPG and ESG classification side by side
  during a cutover, then retire the EPG contracts once every endpoint is tagged
  and matched.
