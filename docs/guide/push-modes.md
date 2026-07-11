# Push modes

`push()` always operates on the **whole design tree**, whichever cursor you
call it on.  Construction never touches the network — transport is injected
here, and only here.  The same call works with a sync
{class}`~niwaki.facade.Niwaki` (returns the result) or an async
{class}`~niwaki.facade.AsyncNiwaki` (returns an awaitable).

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
{class}`~niwaki.exceptions.StagedPushError` carrying plain DNs:

```python
from niwaki.exceptions import StagedPushError

try:
    config.push(aci, mode="staged")
except StagedPushError as exc:
    print("written :", exc.report.dns)
    print("failed  :", [dn for dn, _ in exc.failures])
    print("skipped :", exc.not_run)
```

## `plan` — dry run

Reads the current APIC state (one `rsp-subtree=full` read per declared
domain) and diffs it against the design.  **Nothing is pushed.**  Returns a
{class}`~niwaki.design.PlanResult`:

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
On a loaded fabric, planning `uni/infra` or `uni/fabric` pulls the whole
policy subtree of that domain.  Scope day-2 plans to what you declare, and
prefer several small designs over one giant one when only auditing.
```

## `to_payload()` — inspect without executing

Returns the exact strict-mode payload as a dict (same philosophy as the query
builder's `build()`): validation and reference resolution run, no transport.

```python
import json
print(json.dumps(config.to_payload(), indent=2))
```
