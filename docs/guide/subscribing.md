# Subscribing to live changes

`Query.subscribe()` turns a query into a live push stream: the APIC notifies
your client the moment a matching object is created, modified, or deleted —
no polling. This page covers the API; for the mechanics behind it (one shared
WebSocket per session, no replay after a disconnect, refresh/recovery policy)
see {doc}`../design-first`.

## A live stream

Any query that targets a single class can be subscribed instead of fetched:

<!--- skip: next --->
```python
from niwaki.models.fv.fvBD import fvBD

with aci.query(fvBD).under("uni/tn-prod").subscribe() as sub:
    for bd in sub.initial:            # the synchronous snapshot, first
        print("already there:", bd.dn)
    for event in sub:                 # then the live stream, forever
        print(event.kind, event.dn)
```

`.initial` is the subscribe response's own snapshot — a single page, not the
exhaustive read `fetch()` would give you. Everything after that is push: the
stream never ends on its own, so a real program iterates it from a dedicated
task/thread, or breaks out once it has seen what it needed.

Subscribing to a scope right after creating its parent can miss the very
first push under it: the APIC needs a brief moment after a brand-new object
materializes before it reliably notifies on children created under it. A
scope that already existed is unaffected — this only bites the instant right
after the parent itself was created.

## Typed events

Each item is a `SubscriptionEvent` — `.kind` tells you what happened, `.mo` is
the typed object (deserialised through the same field names as a normal read),
`.dn` and `.subscription_ids` are always available:

```python
from niwaki.query import EventKind

assert EventKind.CREATED == "created"
assert EventKind.MODIFIED == "modified"
assert EventKind.DELETED == "deleted"
```

The APIC push payload is sparse by design, and `event.mo.model_fields_set`
reports exactly what *this* event carried — not what a full read would:

<!--- skip: next --->
```python
for event in sub:
    if event.kind is EventKind.DELETED:
        print("gone:", event.dn)               # event.mo carries no fields
    elif event.kind is EventKind.MODIFIED:
        print("changed:", event.mo.model_fields_set)   # only the changed props
    elif event.kind is EventKind.CREATED:
        print("new:", event.mo.model_fields_set)       # the full object
```

Two kinds carry no object at all — they describe the *subscription*, not the
thing being watched:

- `EventKind.GAP` — the shared socket reconnected (or a subscription
  recovered after missed refreshes) and resubscribed from scratch. The APIC
  has **no replay mechanism at all**, so events raised in the gap are truly
  lost — reconcile with a fresh read if that matters to you.
- `EventKind.REFRESH_FAILED` — a scheduled refresh was rejected. Informational
  on its own; two in a row trigger an automatic recovery (see below).

Neither of these ends the stream — only `SubscriptionLostError` does, and
only once recovery has been tried and failed.

## Refresh, recovery, and when it gives up

A subscription needs periodic refreshing or the APIC lets it expire (60s by
default). This is entirely automatic — nothing here needs a caller-driven
loop — but the policy is worth knowing:

- The client refreshes every 20s by default (a third of the APIC's own
  deadline — override with `subscribe(refresh_timeout=...)`).
- Two **consecutive** missed refreshes trigger an automatic recovery: a fresh
  resubscribe under a new id, delivered as a `GAP` event — not a failure.
- `SubscriptionLostError` is raised only if that recovery resubscribe itself
  fails, or if the shared WebSocket disconnects and cannot be reconnected at
  all. `.reason` tells you which:

```python
from niwaki.exceptions import SubscriptionLostReason

assert SubscriptionLostReason.REFRESH_ESCALATION == "refresh_escalation"
assert SubscriptionLostReason.RECONNECT_EXHAUSTED == "reconnect_exhausted"
assert SubscriptionLostReason.RESUBSCRIBE_FAILED == "resubscribe_failed"
```

## Managing subscriptions in bulk

`aci.subscriptions` reaches every subscription open on the session's shared
socket — useful for a long-running process that wants an occasional health
check without tracking each `Subscription` object itself:

<!--- skip: next --->
```python
for info in aci.subscriptions.list():
    if info.is_stale:                 # at least one recent refresh failure
        print("struggling:", info.path, info.consecutive_refresh_failures)

aci.subscriptions.refresh_all()       # force a refresh sweep now, diagnostically
aci.subscriptions.close_all()         # stop every subscription — the socket stays open
```

`close_all()` is deliberately not the same as closing the client: the shared
WebSocket and its background threads stay alive, so the next `subscribe()`
reuses the same connection instead of reconnecting. A single subscription has
the same two tools scoped to itself — `sub.info` and `sub.refresh_now()`.

## Not subscribable

A stats class (`isStat` in the read catalogue — see {doc}`discovery`) bypasses
the APIC's event manager entirely, so subscribing to one raises
`StatsClassNotSubscribableError` before any network call. Accumulated query
state with no meaning on an open-ended stream — `order_by()`, a slice limit,
subtree enrichment (`include()`, `with_faults()`, …), `also()` — is rejected
the same way, for the same reason: fail before the network, not silently
misrepresent the stream.

## Async

`AsyncQuery.subscribe()` mirrors everything on this page —
`AsyncSubscription` is async-iterable, `aci.subscriptions` is the same
manager with `refresh_all()`/`close_all()` as coroutines. See {doc}`async`.

## Next steps

- {doc}`../cookbook/watch-for-changes` — a small watch-and-print program
- {doc}`../reference/api/query` — `Subscription`, `SubscriptionEvent`,
  `EventKind`, `SubscriptionInfo`
- {doc}`errors` — the full exception hierarchy, including
  `SubscriptionLostError`
