# When a push fails

**Problem** — a push failed, or you do not trust it yet.  This is the diagnostic
ladder, cheapest rung first: inspect the payload offline, plan, read the typed
error, recover from a partial staged push.  Most "why did the APIC reject this?"
questions are answered before you ever reach the wire.

## Rung 1 — `to_payload()`: see what would be sent

No session, no I/O — the exact strict-mode payload, with resolution and
validation already run:

```python
import json

from niwaki.design import tenant

config = tenant("commerce")
config.vrf("prod")
config.bd("bd-web", unicast_routing=True).bind(vrf="prod").subnet("10.30.10.1/24")

print(json.dumps(config.to_payload(), indent=2))
```

Reading the envelope answers most rejection questions — a missing relation, or a
wire value that is not what you assumed.

```{warning}
The payload (and `plan` output) is your fabric's configuration — treat it like
configuration when pasting into tickets and chats.
```

## Rung 2 — resolution errors: read the message

Everything checkable before the wire fails before the wire, with the declared
world named in the message ({doc}`../guide/errors`):

```python
from niwaki.exceptions import DesignError

probe = tenant("commerce")
probe.bd("bd-web").bind(vrf="prd")            # typo — no such VRF declared

try:
    probe.to_payload()
except DesignError as exc:
    print(exc)     # ...no fvCtx named 'prd' is declared... (with a did-you-mean)
```

`to_payload()` triggers the same resolution as `push()`, so it is the offline way
to shake out reference errors.

## Rung 3 — plan: what would actually change?

```python
from niwaki import Niwaki

aci = Niwaki.connect("https://apic.example.com", "admin", "secret")

plan = config.push(aci, mode="plan")
print("creates  :", plan.creates)
print("updates  :", plan.updates)
print("converged:", not plan.has_changes)
```

The `with` form closes the session for you; `connect()` is used here so the
rest of the page can share one client — see {doc}`../guide/connection`.

A plan that proposes *more* than you expected usually means a default you set
explicitly somewhere (every `set()` field is compared), or a name that does not
match the existing object (so it *creates* instead of updating).  The inverse
surprise: write-only attributes never read back, so a rotated password plans as
"unchanged" — expected ({doc}`../guide/push-modes`).

## Rung 4 — the APIC said no

On-wire rejections surface as typed exceptions carrying the APIC's own message.
Log `status_code` and `apic_message` — the latter names the offending attribute
more often than not:

```python
from niwaki.exceptions import APIError

try:
    config.push(aci)
except APIError as exc:
    print(exc.status_code, exc.apic_message)
```

With `strict` mode there is nothing to clean up after a rejection: the POST was
atomic, the fabric did not change.

## Rung 5 — a staged push stopped midway

`staged` mode can partially apply; the exception is the recovery map
({doc}`../guide/errors` has the full playbook):

```python
from niwaki.exceptions import StagedPushError

try:
    config.push(aci, mode="staged")
except StagedPushError as exc:
    print("applied :", exc.report.dns)
    print("failed  :", [dn for dn, _ in exc.failures])
    print("skipped :", exc.not_run)
```

Fix the design, push again — upserts converge, and nothing needs undoing because
the applied objects are exactly what the design describes.

## When it is not the push

- **Auth / TLS / timeouts** — the exception class tells you which; the knobs are
  in {doc}`troubleshooting-connection`.
- **Pushed fine, not working** — the fabric took the config; now audit it: faults
  on the new objects (`aci.query("faultInst").where(severity="critical")`), then
  health ({doc}`fabric-audit`).
- **It worked yesterday** — re-run yesterday's design with `mode="plan"`: the
  diff between intent and fabric *is* the incident summary ({doc}`day-2-changes`).
