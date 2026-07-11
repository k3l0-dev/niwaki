# A three-tier application tenant

**Problem** — provision a classic web / app / database application: one
tenant, one VRF, a bridge domain and subnet per tier, an EPG per tier, and
contracts so that web can reach app, app can reach db, and nothing else.

## The design

The whole application is one design — declarations can come in any order,
and loops are plain Python:

```python
from niwaki import Niwaki
from niwaki.design import tenant

config = tenant("shop")
config.vrf("prod")

tiers = {"web": "10.20.1.1/24", "app": "10.20.2.1/24", "db": "10.20.3.1/24"}
for name, gateway in tiers.items():
    config.bd(name, unicast_routing=True).subnet(gateway).bind(vrf="prod")

app = config.app("storefront")
app.epg("web").bind(bd="web").consume("web-to-app")
app.epg("app").bind(bd="app").provide("web-to-app").consume("app-to-db")
app.epg("db").bind(bd="db").provide("app-to-db")

config.filter("http-8080").entry("http", tcp=8080)
config.filter("postgres").entry("pg", tcp=5432)
config.contract("web-to-app").set(scope="vrf").subject("http").bind(filter="http-8080")
config.contract("app-to-db").set(scope="vrf").subject("sql").bind(filter="postgres")
```

Every `bind()`, `provide()` and `consume()` is a lazy reference into the
same design: a typo in any of them fails at push time with a did-you-mean,
before any request is made.

## Plan first

```python
aci = Niwaki.connect("https://apic.example.com", "admin", "secret")

plan = config.push(aci, mode="plan")
print(f"{len(plan.creates)} objects to create")
```

On an empty fabric the plan lists every DN the design would create:

```text
21 objects to create
```

## Push

One atomic POST — the application lands entirely or not at all:

```python
report = config.push(aci)
assert report.request_count == 1
```

## Verify

```python
bd = aci.tenant("shop").bd("web").read()
assert bd.unicast_routing is True

epgs = aci.tenant("shop").query("fvAEPg").fetch()
assert len(epgs) == 3

plan = config.push(aci, mode="plan")
assert plan.has_changes is False          # the design is converged
```

That last check is the declarative payoff: the same object that provisioned
the application is also its drift detector.

## Variations & pitfalls

- **Contract scope** — `scope="vrf"` (wire value `context`) keeps the
  contract local to the VRF; the default `global` scope leaks intent across
  VRFs.  Decide explicitly.
- **Shared services** — a `db` tier consumed by several applications
  usually deserves its own application profile and a contract per consumer;
  the design stays one file either way.
- **Growing a tier** — day-2 additions are smaller designs: a new subnet is
  `tenant("shop").bd("web").subnet("10.20.4.1/24").push(aci)` — the parent
  chain rides along as attribute-less upserts ({doc}`../guide/design-dsl`).
