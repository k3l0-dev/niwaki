# Day-2 changes, the declarative way

**Problem** — the database tier is going into an active/standby cluster that
fails its virtual IP over with gratuitous ARP.  For that to work, `bd-db` needs
ARP flooding turned on.  It is a one-field change on a bridge domain that already
exists — and the point of this recipe is how small, safe, and reviewable that is.

There is no `update()` in the SDK.  A day-2 change is just a **smaller design**:
declare only what changes, and the parent chain travels as attribute-less
upserts that touch nothing.

## Starting point

The `bd-db` bridge domain from {doc}`onboard-tenant`, already on the fabric with
ARP flooding off (the default):

```python
from niwaki import Niwaki
from niwaki.design import tenant

aci = Niwaki.connect("https://apic.example.com", "admin", "secret")

baseline = tenant("commerce")
baseline.vrf("prod")
baseline.bd("bd-db", unicast_routing=True).bind(vrf="prod").subnet("10.30.30.1/24")
baseline.push(aci)
```

The `with` form closes the session for you; `connect()` is used here so the
rest of the page can share one client — see {doc}`../guide/connection`.

## The change

Declare the one field.  The tenant and BD are named but carry no attributes, so
they ride along as upserts:

```python
change = tenant("commerce").bd("bd-db").set(arp_flooding=True)
```

`set()` is how you configure an object that is already declared — here, in a
design whose only job is this field.

## Plan — the safety net

The plan says precisely what will change, and nothing else:

```python
plan = change.push(aci, mode="plan")

dn = "uni/tn-commerce/BD-bd-db"
assert plan.updates[dn] == {"arp_flooding": (False, True)}
assert plan.creates == []
```

One field, from `False` to `True`.  This is the review artifact — in a pipeline
it is what lands in the merge request ({doc}`gitops-pipeline`).

## Push, verify, converge

```python
change.push(aci)

bd = aci.tenant("commerce").bd("bd-db").read()
assert bd.arp_flooding is True

assert change.push(aci, mode="plan").has_changes is False
```

## Variations & pitfalls

- **Only what you `set()` travels** — attributes you never touched are never in
  the payload and never reported as drift; the change is exactly as wide as you
  declared it.
- **Parents are upserts, not rewrites** — declaring `tenant("commerce")` again
  does not reset the tenant; a name-only maker is a no-op upsert.  This is why a
  day-2 design can be three lines.
- **Secrets are write-only** — rotating a password or pre-shared key plans as
  "unchanged", because the APIC never reads secrets back.  Push to apply the new
  value; do not expect the plan to show it ({doc}`../guide/push-modes`).
- **Removals are their own change** — a design never deletes what it does not
  declare.  Retiring the subnet is a separate, explicit
  `aci.node("uni/tn-commerce/BD-bd-db/subnet-[10.30.30.1/24]").delete()` after
  the last consumer has moved ({doc}`fabric-audit`).
