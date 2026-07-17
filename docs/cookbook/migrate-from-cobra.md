# Migrate from cobra

**Problem** — you have working `cobra` automation and want to port it.  This is
the practical mapping: which cobra concept becomes which niwaki call, three
worked ports against the `commerce` deployment, and the pitfalls that bite in
practice.  For the argument, see {doc}`../why`; for the same tasks in both SDKs
side by side, {doc}`../comparison`.

cobra snippets follow the [official documentation](https://cobra.readthedocs.io/)
and are shown for reference — cobra is not installable from an index, so they are
not executed here.  The niwaki blocks run.

## The concept map

| cobra | niwaki |
| --- | --- |
| `LoginSession` + `MoDirectory` + `login()` | `Niwaki(...)` context manager ({doc}`../guide/connection`) |
| build MOs with parent plumbing (`Tenant(uniMo, 'x')`) | designs — detached trees, no parent objects needed ({doc}`../guide/design-dsl`) |
| relation classes + `tn*Name` strings (`RsCtx(bd, tnFvCtxName='v')`) | `bind(vrf="v")` — relation class, direction and target prop derived |
| `ConfigRequest` + `addMo()` + `commit()` | `config.push(aci)` — `strict` / `staged` / `plan` ({doc}`../guide/push-modes`) |
| `lookupByDn('uni/tn-x')` | `aci.node("uni/tn-x").read()` or vocabulary navigation |
| `lookupByClass(...)` / `ClassQuery` + `propFilter` strings | `aci.query(cls).where(...)` — typed builder ({doc}`fabric-audit`) |
| `DnQuery` + `queryTarget='children'/'subtree'` | `.under(dn)` / node-scoped `query()` |
| manual `page` / `pageSize` loops | transparent pagination; `stream()` for iteration |
| check-after-commit | `plan` dry run + eager validation ({doc}`troubleshooting`) |

## Port 1 — provisioning a tenant

cobra, per the official examples:

<!--- skip: next --->
```python
from cobra.mit.access import MoDirectory
from cobra.mit.session import LoginSession
from cobra.mit.request import ConfigRequest
from cobra.model.fv import Tenant, Ctx, BD, RsCtx

ls = LoginSession('https://apic.example.com', 'admin', 'secret')
moDir = MoDirectory(ls)
moDir.login()

uniMo = moDir.lookupByDn('uni')
tenantMo = Tenant(uniMo, 'commerce')
Ctx(tenantMo, 'prod')
bdMo = BD(tenantMo, 'bd-web')
RsCtx(bdMo, tnFvCtxName='prod')

req = ConfigRequest()
req.addMo(tenantMo)
moDir.commit(req)
moDir.logout()
```

The same, ported:

```python
from niwaki import Niwaki
from niwaki.design import tenant

config = tenant("commerce")
config.vrf("prod")
config.bd("bd-web").bind(vrf="prod")

with Niwaki("https://apic.example.com", "admin", "secret") as aci:
    config.push(aci)
```

What disappeared: the `uni` lookup (designs are detached — no round trip before
writing), the relation class and its `tnFvCtxName` string (derived from the
schemas), and the request object.  What appeared: closed-world checking —
misspell `prod` in the `bind()` and the push fails *before any request*, with a
did-you-mean.  Note the `bind()` sits on the BD itself, the object that owns the
relation.

## Port 2 — a filtered class query

<!--- skip: next --->
```python
tenants = moDir.lookupByClass(
    "fvTenant", propFilter='and(eq(fvTenant.name, "commerce"))'
)
```

```python
aci = Niwaki.connect("https://apic.example.com", "admin", "secret")

config = tenant("commerce")
config.vrf("prod")
config.push(aci)

tenants = aci.query("fvTenant").where(name="commerce").fetch()
assert [t.name for t in tenants] == ["commerce"]
```

The `with` form closes the session for you; `connect()` is used here so the
rest of the page can share one client — see {doc}`../guide/connection`.

Filter kwargs address the APIC attribute names, exactly like the `propFilter`
string did — but composed and quoted for you, with `and_` / `or_` / `gt`
expressions when kwargs are not enough ({doc}`fabric-audit`).

## Port 3 — check-before-write

cobra has one write verb, `commit()`; verification means committing and reading
back.  The niwaki port turns that inside out — the check comes *first*:

```python
change = tenant("commerce").bd("bd-web").set(arp_flooding=True)

plan = change.push(aci, mode="plan")
print(plan.updates or plan.creates)     # review artifact — nothing written yet

change.push(aci)
assert change.push(aci, mode="plan").has_changes is False
```

## Pitfalls when porting

- **No ancestor climb on `bind()`** — a bind attaches to the current cursor, not
  a parent.  Port `RsCtx(bdMo, ...)` to `bd.bind(vrf=...)` on the BD, then descend
  to children (`.subnet(...)`) after the bind — not before.
- **Wire names live on in two places** — query filters and `to_payload()` output
  speak APIC (`arpFlood`); everything else speaks operator (`arp_flooding`).  A
  ported `propFilter` keeps its attribute names.
- **No implicit parents** — cobra required a live parent MO (`lookupByDn('uni')`);
  designs declare the chain instead, and attribute-less parents are upserts that
  touch nothing.  Do not port the lookups.
- **`commit()` batches ≠ `staged`** — one `ConfigRequest` is closest to `strict`
  (one POST); use `staged` only when you *want* per-object progress and its
  partial-failure semantics ({doc}`troubleshooting`).
- **Firmware coupling is gone** — your venv no longer tracks the APIC.  The models
  ship with the package (APIC 6.0 schemas); string-name *queries* still work for
  classes newer than the shipped schema (`aci.query("newClass")`).
- **Session hygiene is automatic** — delete the `login()` / `logout()` / re-auth
  plumbing; the context manager and proactive token refresh own it
  ({doc}`../guide/connection`).
