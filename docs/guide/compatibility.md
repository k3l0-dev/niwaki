# Compatibility

The first question an evaluating team asks — "we run APIC 5.2/6.1, does
this work?" — deserves a precise answer, not a shrug. This page states
exactly what niwaki is validated against and how it behaves outside that
envelope.

## APIC firmware

**niwaki is generated from, and validated against, APIC 6.0(9c).** The
2,222 typed models, the read catalogue (15,452 classes), the navigation
vocabulary and the filter grammar all derive from that firmware's schemas,
and the whole configuration surface is exercised live against a 6.0(9c)
fabric on every release cycle (10,000+ pushed objects, the full query
grammar, WebSocket subscriptions).

Outside 6.0(9c), the behavior is asymmetric by design:

- **Reads are tolerant.** Any class the APIC returns is readable —
  `aci.query("someClass")` works by name, results expose `.dn`,
  `mo["wireAttr"]` and `.attrs` regardless of whether a typed model or a
  catalogue entry exists. A firmware that *adds* classes or attributes
  degrades discovery (`catalog.describe` raises `UnknownClassError` for
  classes newer than the snapshot; unknown wire attributes stay reachable
  by their wire name), never correctness.
- **Writes are fail-loud.** The design DSL validates against the 6.0(9c)
  schemas before anything reaches the wire, and the APIC itself rejects
  what its firmware does not know — with the DN in clear in `staged`
  mode. Pushing 6.0-only attributes to an older APIC fails visibly, never
  silently.
- **Older firmware (5.x):** the core configuration classes (tenants,
  BDs, EPGs, contracts, access policies) have been wire-stable for many
  releases and generally work, but this is not systematically validated —
  pilot on a lab fabric first, exactly as you would with any tool.
- **Newer firmware (6.1+):** everything 6.0(9c) knows keeps working;
  additions are invisible to the typed surface until niwaki ships a
  schema refresh (the generation pipeline is built to be re-run per
  firmware, so refreshes are mechanical).

The APIC version the shipped catalogue was built from is embedded in the
artifact itself and checked by the test suite.

## Python

- **Requires Python 3.12+** (PEP 695 generics, `warnings` API additions).
- **3.12 and 3.13** are fully tested in CI on every commit and covered by
  the offline wheelhouse attached to each GitHub release.
- **3.14** is built and observed in CI (non-blocking) — it works today,
  and becomes a supported classifier once it is validated as blocking.

## Thread and task safety

- **`Niwaki` / `ApicSession` (sync):** safe to share across threads. The
  underlying HTTP client is thread-safe, token refresh is serialized (a
  401 triggers exactly one re-login however many threads race it), and
  the read catalogue serves concurrent readers from one shared connection
  without a lock. Each
  `Subscription` should be consumed by one thread; the shared WebSocket
  machinery behind them is thread-safe.
- **`AsyncNiwaki` (async):** one instance per event loop. Fan out with
  {meth}`~niwaki.AsyncNiwaki.gather`, which runs its awaitables under a
  single `TaskGroup` on the session's loop.
- **Designs** are plain object trees: build them anywhere, push them
  through whichever connected client you like — they hold no transport
  state.

## See also

- {doc}`installation` — including air-gapped installs from the wheelhouse
- {doc}`errors` — the exception hierarchy the fail-loud behavior speaks
