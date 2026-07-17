# Stand up a multi-tier application

**Problem** — the `commerce` tenant now needs its storefront: a web tier that
talks to an app tier, an app tier that talks to a database, and **nothing
else**.  In ACI that intent is EPGs plus contracts — the whitelist model, where
traffic is denied until a contract permits it.  One design expresses the whole
policy.

This builds directly on {doc}`onboard-tenant` — same tenant, same VRF, same
bridge domains.

## The design

Declarations can come in any order and loops are plain Python.  Each EPG binds
its bridge domain and states which contracts it **provides** (offers a service)
and **consumes** (uses a service):

```python
from niwaki import Niwaki
from niwaki.design import tenant

config = tenant("commerce")
config.vrf("prod")
config.bd("bd-web", unicast_routing=True).bind(vrf="prod").subnet("10.30.10.1/24")
config.bd("bd-app", unicast_routing=True).bind(vrf="prod").subnet("10.30.20.1/24")
config.bd("bd-db", unicast_routing=True).bind(vrf="prod").subnet("10.30.30.1/24")

app = config.app("storefront")
app.epg("web").bind(bd="bd-web").consume("web-to-app")
app.epg("app").bind(bd="bd-app").provide("web-to-app").consume("app-to-db")
app.epg("db").bind(bd="bd-db").provide("app-to-db")
```

`provide` and `consume` are **verbs**, not binds: an EPG both provides and
consumes contracts, so there is no way to infer the direction from the target
alone — the verb names it ({doc}`../guide/cursors`).

Now the contracts themselves.  A contract carries subjects; a subject binds the
filters that classify traffic.  The `tcp=` sugar expands to the right
destination-port attributes:

```python
config.filter("f-http").entry("http", tcp=8080)
config.filter("f-postgres").entry("pg", tcp=5432)

config.contract("web-to-app").set(scope="vrf").subject("http").bind(filter="f-http")
config.contract("app-to-db").set(scope="vrf").subject("sql").bind(filter="f-postgres")
```

Every `bind()`, `provide()` and `consume()` resolves inside this one design:
misspell a contract in any of them and the push fails **before any request**,
with a did-you-mean.

## Plan

```python
aci = Niwaki.connect("https://apic.example.com", "admin", "secret")

plan = config.push(aci, mode="plan")
print(f"{len(plan.creates)} objects to create")
```

The `with` form closes the session for you; `connect()` is used here so the
rest of the page can share one client — see {doc}`../guide/connection`.

## Push

```python
report = config.push(aci)
assert report.request_count == 1
```

## Verify

The EPGs exist, and the contract graph is what you declared:

```python
epgs = aci.tenant("commerce").query("fvAEPg").fetch()
assert {e.name for e in epgs} == {"web", "app", "db"}

provided = aci.tenant("commerce").query("fvRsProv").fetch()
assert {p.name for p in provided} == {"web-to-app", "app-to-db"}

assert config.push(aci, mode="plan").has_changes is False
```

## Variations & pitfalls

- **Contract scope is a decision** — `scope="vrf"` (wire value `context`) keeps
  the contract inside the VRF.  The default `global` scope leaks the intent
  across VRFs; choose explicitly, especially for shared services.
- **Reuse over duplication** — a database consumed by several apps deserves one
  contract that each consumer references by name, not a copy per app; the design
  stays one file either way.
- **Filters are directional at the entry** — `tcp=8080` sets the *destination*
  port, which is what a client-to-server rule wants.  A contract applies in both
  directions unless you split it into `in`/`out` terms.
- **Contract logging** lives on the subject-to-filter attachment: wrap the
  filter in `ref()` to turn it on —
  `.subject("http").bind(filter=ref("f-http", directives="log"))`
  ({doc}`../guide/design-dsl`).
