# Push modes

`push()` always operates on the **whole design tree**, whichever cursor you
call it on.  Construction never touches the network — transport is injected
here, and only here.  The same call works with a sync
{class}`~niwaki.facade.Niwaki` (returns the result) or an async
{class}`~niwaki.facade.AsyncNiwaki` (returns an awaitable).

The examples on this page share one design and one connected client:

```python
from niwaki import Niwaki
from niwaki.design import tenant

config = tenant("prod").vrf("main")
aci = Niwaki.connect("https://apic.example.com", "admin", "secret")
```

The `with` form closes the session for you; `connect()` is used here so the
rest of the page can share one client — see {doc}`connection`.

```python
report = config.push(aci)                  # strict (default)
report = config.push(aci, mode="staged")
plan   = config.push(aci, mode="plan")
```

## `strict` — one atomic POST

Closed-world validation, then a single nested POST of the whole design to
`/api/mo/uni.json`.  The APIC applies it **all or nothing**: any invalid
object rolls back the entire request.  Returns a
{class}`~niwaki.design.PushReport` with the covered DNs and
`request_count == 1`.

This is the default because it matches the declarative promise: the design
either lands entirely or not at all.

## `staged` — waves of per-object requests

The design is compiled to one operation per object and executed in **waves by
DN depth** — parents always land before children; within a wave, the async
client runs operations concurrently.  Classes the APIC validates as a whole
(a vPC pair with its two node endpoints) ship their subtree in a single
nested operation.

Use it when you want progress granularity, or when a fabric rejects large
atomic envelopes.  A partial failure raises
{class}`~niwaki.exceptions.StagedPushError` — what it carries and how to
recover is the subject of the {doc}`errors` playbook.

## `plan` — dry run

Reads the current APIC state and diffs it against the design.  **Nothing is
pushed.**  There is one read per declared domain (each direct child of
`polUni` the design touches), and each read is scoped twice:
`rsp-subtree=full` fetches the hierarchy, and `rsp-subtree-class` restricts
it to **the classes the design declares** — planning a three-line `infra`
design against a loaded fabric reads back a handful of objects, not the
whole access-policy tree.  Returns a {class}`~niwaki.design.PlanResult`:

```python
plan = config.push(aci, mode="plan")
plan.creates      # DNs that do not exist yet
plan.updates      # {dn: {field: (current, desired)}}
plan.unchanged    # DNs already matching
plan.has_changes  # False → the design is fully converged
```

Only the fields the design actually declares are compared — an attribute you
never set is never reported as drift.  Deletions are out of scope by design:
a plan never proposes removing objects the design does not declare.

```{note}
Write-only attributes (passwords, pre-shared keys) never read back from the
APIC, so a plan cannot see them: after rotating a secret, the plan reports
the object as unchanged — push to apply the new value.
```

Several small designs still beat one giant one — not for the APIC's sake,
but because each plan then reads as one reviewable change.

## `to_payload()` — inspect without executing

Returns the exact strict-mode payload as a dict (same philosophy as the query
builder's `build()`): validation and reference resolution run, no transport.

```python
import json
print(json.dumps(config.to_payload(), indent=2))
```

## Next steps

- {doc}`errors` — the exception hierarchy and the staged-failure playbook
- {doc}`testing` — the plan as a convergence assertion
- {doc}`../cookbook/gitops-pipeline` — plan as a CI gate
