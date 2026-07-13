# Compatibility & limits

The facts to check before betting an automation project on niwaki — what it
runs on, what it covers, and where its semantics deliberately stop.

## Runtimes and fabrics

| Surface | Supported |
| --- | --- |
| Python | 3.12 and 3.13 (typed, `Typing :: Typed`) |
| APIC schemas | Models generated from the APIC **6.0** schema release |
| Older / newer firmware | Reads by class name work on any firmware (`aci.query("topSystem")`); typed models and `.mo()` cover the 2,222 generated classes of the tracked release |

A class newer than the tracked schema release is still reachable — query it
by name and read the attributes from the raw payload; it just has no typed
model or curated position yet.

## Vocabulary coverage

The design DSL curates {{ positions }} positions — the {doc}`DSL reference
<vocabulary/index>` documents each one field by field, and the generated
{doc}`coverage matrix <vocabulary/coverage>` lists them all.  Everything
outside the curated vocabulary stays writable through `.mo(AnyClass, ...)`
and referenceable through `bind_dn(alias=dn)` — coverage limits ergonomics,
never reach.

## Semantics — deliberate limits

- **No desired-state reconciliation.**  A design only creates and updates
  what it declares; it never deletes what it does not mention.  Deletion is
  an explicit, imperative act (`aci.tenant("x").delete()`), always your
  decision.
- **`plan` compares declared fields only.**  An attribute the design never
  sets is never reported as drift — and never reverted.
- **`plan` cannot see write-only attributes.**  Passwords and pre-shared
  keys are never echoed by the APIC; after rotating one, the plan reports
  the object unchanged.  Rotating a secret means pushing it.
- **`staged` can land partially.**  Waves that succeeded stay on the
  fabric; the failure carries exactly what was written, what failed and
  what never ran ({doc}`../guide/errors`).

## Transport

- The sync session is **not thread-safe** — one session per thread, or use
  {class}`~niwaki.AsyncNiwaki` (bounded concurrency, default
  `max_concurrent=10`).
- Very large designs may exceed what a fabric accepts as one atomic
  envelope — `mode="staged"` ships the same design as per-object waves
  ({doc}`../guide/push-modes`).

## Next steps

- {doc}`../guide/installation` — including air-gapped installs
- {doc}`vocabulary/coverage` — the generated coverage matrix
