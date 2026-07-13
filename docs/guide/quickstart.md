# Quickstart

## Install

`uv add niwaki` (or `pip install niwaki`) — Python 3.12+.  Restricted
network?  Every release ships an offline wheelhouse: {doc}`installation`.

## Connect

{class}`~niwaki.Niwaki` (sync) and {class}`~niwaki.AsyncNiwaki` (async) are
context managers — authentication happens on entry, the session closes on
exit.  Credentials fall back to the `APIC_HOST` / `APIC_USERNAME` /
`APIC_PASSWORD` environment variables when omitted.

```python
from niwaki import Niwaki

with Niwaki("https://apic.example.com", "admin", "secret") as aci:
    ...
```

## Describe, apply, observe

Build a detached design — no session, no I/O, every name and attribute
validated at the call site — then push it:

```python
from niwaki.design import tenant

config = tenant("prod", description="my first tenant")
config.vrf("main")
config.bd("web", unicast_routing=True).subnet("10.0.1.1/24").bind(vrf="main")

with Niwaki("https://apic.example.com", "admin", "secret") as aci:
    plan = config.push(aci, mode="plan")     # dry run — nothing written
    print(plan.creates)                      # every DN that would be created

    config.push(aci)                         # one atomic POST

    bd = aci.tenant("prod").bd("web").read() # observe it back
    assert bd.unicast_routing is True
```

Three things happened that are easy to miss:

- `bind(vrf="main")` recorded a **lazy reference**: at push time the resolver
  found the declared VRF and built the `fvRsCtx` relation — you never wrote a
  relation class or a `tn*Name` prop.
- `mode="plan"` read the current APIC state and diffed it against the design
  — the same design is your dry run, your apply, and your drift check.
- The read went through the **facade**, which only observes.  There is no
  `create()` on the facade: configuration always goes through a design.

## Day-2 changes are small designs

Declare the field you want; parent objects ride along as attribute-less
upserts and nothing else is touched:

```python
from niwaki.design import infra

flip = infra().cdp_policy("cdp-on", admin_state="disabled")

with Niwaki("https://apic.example.com", "admin", "secret") as aci:
    flip.push(aci, mode="plan")   # shows exactly one field changing
    flip.push(aci)
```

## Next steps

- {doc}`design-dsl` — the full describe surface: makers, binds, escapes.
- {doc}`push-modes` — `strict`, `staged`, `plan` in detail.
- {doc}`observing` — navigation, typed reads, and the query builder.
- {doc}`../reference/vocabulary/index` — every position, maker, keyword
  argument and bind alias, generated from the vocabulary itself.
