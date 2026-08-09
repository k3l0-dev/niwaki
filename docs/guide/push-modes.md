# Push modes

`push()` always operates on the **whole design tree**, whichever cursor you
call it on.  Construction never touches the network — transport is injected
here, and only here.  The same call works with a sync
{class}`~niwaki.Niwaki` (returns the result) or an async
{class}`~niwaki.AsyncNiwaki` (returns an awaitable).

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
report = config.push(aci)  # strict (default)
report = config.push(aci, mode="staged")
plan = config.push(aci, mode="plan")
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

How many operations of a wave run at once is the client's `max_concurrent`
(default `10`), which `push(..., max_concurrent=n)` can narrow for a single
push — never widen.  The DNs in the report follow the design, not the order
the controller answered in.

Use it when you want progress granularity, or when a fabric rejects large
atomic envelopes.  A partial failure raises
{class}`~niwaki.exceptions.StagedPushError` — what it carries and how to
recover is the subject of the {doc}`errors` playbook.

## `plan` — dry run

Reads the current APIC state and diffs it against the design.  **Nothing is
pushed.**  There is one read per declared domain (each direct child of
`polUni` the design touches), scoped to **the classes the design declares** —
planning a three-line `infra` design against a loaded fabric reads back a
handful of objects, not the whole access-policy tree.  A design large enough
that its class list would overflow the controller's URL limit is read in
several smaller requests automatically; either way the SDK reassembles the
hierarchy before diffing.  Returns a {class}`~niwaki.design.PlanResult`:

```python
plan = config.push(aci, mode="plan")
plan.creates  # DNs that do not exist yet
plan.updates  # {dn: {field: (current, desired)}}
plan.unchanged  # DNs already matching
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

## `verify_refs` — check external references before writing

A design may reference objects it does not declare: `bind_dn` targets and
the literal-DN makers (`static_path`, `path_attachment`, …) carry raw DNs.
The APIC *accepts* a relation whose target does not exist — the config
lands, the relation stays unformed, and a fault is the only trace.
`verify_refs=True` closes that gap: every external DN is read from the APIC
**before anything is written** (reads only), and a missing or wrong-class
target raises with the complete failure list:

```python
from niwaki import exceptions
from niwaki.design import design

dom = design().phys_dom("prod-phys")
dom.bind_dn(vlan_pool="uni/infra/vlanns-[missing]-static")

try:
    dom.push(aci, verify_refs=True)
except exceptions.DanglingReferenceError as exc:
    for check in exc.failures:
        print(check.ref.dn, check.status)  # nothing was pushed
```

In `plan` mode the statuses land on `PlanResult.external_refs` instead —
plan is the warn tier and never raises for a dangling reference. Targets
the design itself declares are skipped (this very push creates them), and
verification cannot catch a target deleted *between* the check and the
POST — that window stays inherent.

Measured on APIC 6.0(9c): a dangling static path pushed without the flag
is accepted with the relation left `state=unformed` — and on the
simulator **no fault is ever raised**, so the unformed field is the only
trace. Verification reads cost ~60 ms per unique DN (24 references in
1.5 s), deduplicated across the design.

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
