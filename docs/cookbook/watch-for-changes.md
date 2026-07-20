# Watch for changes

**Problem** — `commerce` is provisioned and audited ({doc}`fabric-audit`), but
some changes need to be *seen* the moment they happen — a bridge domain
appearing under `tn-commerce`, say — instead of waiting for the next poll.
`Query.subscribe()` opens a live push stream instead of a one-off read; see
{doc}`../guide/subscribing` for the full model (recovery policy, bulk
tools, async). This recipe is deliberately small: **watch and print**.
Alerting, reconciliation, and automation on top of the stream are your
application's job, not the SDK's.

## A minimal watcher

<!--- skip: next --->
```python
from niwaki import Niwaki
from niwaki.models.fv.fvBD import fvBD
from niwaki.query import EventKind

with Niwaki("https://apic.example.com", "admin", "secret") as aci:
    with aci.query(fvBD).under("uni/tn-commerce").subscribe() as sub:
        print(f"watching {len(sub.initial)} existing BD(s)")
        for event in sub:
            match event.kind:
                case EventKind.CREATED:
                    print(f"+ {event.dn}")
                case EventKind.MODIFIED:
                    print(f"~ {event.dn} changed: {sorted(event.mo.model_fields_set)}")
                case EventKind.DELETED:
                    print(f"- {event.dn}")
                case EventKind.GAP:
                    print("! reconnected — events during the gap were not replayed")
                case EventKind.REFRESH_FAILED:
                    print("! a scheduled refresh was rejected (informational)")
```

This runs forever — `Ctrl-C` it, or wrap the loop in whatever lifecycle your
program already has (a task, a thread, a `contextlib.ExitStack`).

## A health-checked long-runner

A process that watches for hours or days should occasionally check on itself
rather than trust the automatic refresh sweep blindly:

<!--- skip: next --->
```python
import time

with Niwaki("https://apic.example.com", "admin", "secret") as aci:
    sub = aci.query(fvBD).under("uni/tn-commerce").subscribe()
    last_check = time.monotonic()

    for event in sub:
        print(event.kind, event.dn)

        if time.monotonic() - last_check > 300:  # every 5 minutes
            if sub.info.is_stale:
                print("this subscription has a recent refresh failure")
            last_check = time.monotonic()
```

## Watching several classes

One socket, several subscriptions — open one per class and multiplex them
yourself (a subscription is typed to a single class on purpose; see
{doc}`../guide/subscribing`):

<!--- skip: next --->
```python
import threading

from niwaki.models.fv.fvBD import fvBD
from niwaki.models.fv.fvAEPg import fvAEPg


def watch(aci: Niwaki, cls: type, tenant_dn: str) -> None:
    with aci.query(cls).under(tenant_dn).subscribe() as sub:
        for event in sub:
            print(cls.__name__, event.kind, event.dn)


with Niwaki("https://apic.example.com", "admin", "secret") as aci:
    threads = [
        threading.Thread(target=watch, args=(aci, cls, "uni/tn-commerce"), daemon=True)
        for cls in (fvBD, fvAEPg)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
```

## Variations & pitfalls

- **The stream can outlive individual events being lost** — a `GAP` means the
  subscription recovered, but anything raised during the gap is gone for
  good (no replay mechanism exists). Re-read if you need to know what you
  missed.
- **`SubscriptionLostError` ends the stream** — catch it around the `for`
  loop if your program should resubscribe rather than exit; `.reason` tells
  you which recovery path was exhausted.
- **Don't build alerting on top of this page's shape** — a real notification
  pipeline wants debouncing, batching, and delivery guarantees this SDK does
  not attempt to provide; keep that logic in your own application layer.
