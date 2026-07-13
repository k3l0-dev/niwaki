# Troubleshooting a push

**Problem** — a push failed, or you do not trust it yet.  This recipe is the
diagnostic ladder, from cheapest to heaviest: inspect the payload, plan,
read the typed error, recover from a partial staged push.

## Rung 1 — `to_payload()`: see what would be sent

No session, no I/O — the exact strict-mode payload, resolution and
validation included:

```python
import json

from niwaki import Niwaki
from niwaki.design import tenant

config = tenant("shop")
config.vrf("prod")
config.bd("web", unicast_routing=True).bind(vrf="prod")

payload = json.dumps(config.to_payload(), indent=2)
print(payload)
```

Most "why is the APIC rejecting this" questions are answered by reading the
envelope — a missing relation or a wire value that is not what you assumed.

```{warning}
The payload (and `plan` output) is your fabric's configuration — treat it
like configuration when pasting into tickets and chats.
```

## Rung 2 — resolution errors: read the message

Everything checkable before the wire fails before the wire, with the
declared world in the message ({doc}`../guide/errors`):

```python
from niwaki.exceptions import DesignError

probe = tenant("shop")
probe.bd("web").bind(vrf="prd")            # typo

try:
    probe.to_payload()
except DesignError as exc:
    print(exc)     # …no fvCtx named 'prd' is declared… (with a did-you-mean)
```

`to_payload()` triggers the same resolution as `push()` — it is the
offline way to shake out reference errors.

## Rung 3 — plan: what would actually change?

```python
aci = Niwaki.connect("https://apic.example.com", "admin", "secret")

plan = config.push(aci, mode="plan")
print("creates  :", plan.creates)
print("updates  :", plan.updates)
print("converged:", not plan.has_changes)
```

The `with` form closes the session for you; `connect()` is used here so the
rest of the page can share one client — see {doc}`../guide/connection`.

A plan that proposes more than you expected usually means a default you
set explicitly somewhere (every `set()` field is compared) or a name that
does not match the existing object (so it *creates* instead of updating).
The inverse surprise: secure, write-only attributes never appear in a plan —
a rotated password planning as "unchanged" is expected
({doc}`../guide/push-modes`).

## Rung 4 — the APIC said no

On-wire rejections surface as typed exceptions carrying the APIC's own
message — log `status_code` and `apic_message`, the latter names the
offending attribute more often than not:

```python
from niwaki.exceptions import APIError

try:
    config.push(aci)
except APIError as exc:
    print(exc.status_code, exc.apic_message)
```

With `strict` mode there is nothing to clean up after a rejection: the POST
was atomic, the fabric did not change.

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

Fix the design, push again — upserts converge; nothing needs undoing.

## When it is not the push

- **Auth / TLS / timeouts** — the exception class tells you which
  ({doc}`../guide/connection` for the knobs: `verify_ssl`, `timeout`,
  `RetryConfig`).
- **Pushed fine, not working** — the fabric took the config; now audit it:
  faults on the new objects
  (`aci.query("faultInst").where(severity="critical")`), then health
  ({doc}`fabric-audit`).
- **It worked yesterday** — re-run yesterday's design with `mode="plan"`:
  the diff between intent and fabric *is* the incident summary.
