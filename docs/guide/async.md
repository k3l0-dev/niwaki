# Async patterns

{class}`~niwaki.facade.AsyncNiwaki` is a strict mirror of the sync client:
same navigation, same query builder, same `push()` — accumulators stay
synchronous, executors become awaitable.  If you know the sync API, you
already know this page; what it adds is the concurrency model.

## The mirror

```python
import asyncio

from niwaki import AsyncNiwaki
from niwaki.design import tenant

config = tenant("prod")
config.bd("web", unicast_routing=True).bind(vrf="main")
config.vrf("main")


async def apply() -> None:
    async with AsyncNiwaki("https://apic.example.com", "admin", "secret") as aci:
        await config.push(aci)                    # designs are transport-agnostic
        bd = await aci.tenant("prod").bd("web").read()
        assert bd.unicast_routing is True


asyncio.run(apply())
```

The same design pushes through either client: transport is injected at
`push()` time, nothing about a design is sync or async.

## Fan-out with `gather()`

{meth}`~niwaki.facade.AsyncNiwaki.gather` runs several awaitables under one
`asyncio.TaskGroup` and returns their results in order — the idiomatic shape
for read fan-out:

```python
async def snapshot() -> None:
    async with AsyncNiwaki("https://apic.example.com", "admin", "secret") as aci:
        tenants, bds, faults = await aci.gather(
            aci.query("fvTenant").fetch(),
            aci.query("fvBD").fetch(),
            aci.query("faultInst").fetch(),
        )
        print(len(tenants), "tenants,", len(bds), "BDs,", len(faults), "faults")


asyncio.run(snapshot())
```

Two properties come from the TaskGroup:

- **Structured concurrency** — nothing leaks: when `gather()` returns, every
  task is finished.
- **Grouped failures** — if several coroutines fail, all failures are
  collected into an `ExceptionGroup` rather than the first one masking the
  rest:

```python
from niwaki.exceptions import NiwakiError


async def audit() -> None:
    async with AsyncNiwaki("https://apic.example.com", "admin", "secret") as aci:
        try:
            tenants, nodes = await aci.gather(
                aci.query("fvTenant").fetch(),
                aci.query("topSystem").fetch(),
            )
        except* NiwakiError as group:
            for exc in group.exceptions:
                print("failed:", exc)


asyncio.run(audit())
```

## Concurrency limits

The client holds a session-level semaphore (`max_concurrent`, default `10`):
however many coroutines you fan out, at most that many requests are in
flight against the APIC.  Token refreshes are serialised internally, so a
hundred concurrent reads never race a re-login.

```python
wide = AsyncNiwaki("https://apic.example.com", "admin", "secret", max_concurrent=20)
```

Retries compose with the limit: a retrying request holds its semaphore slot,
so a struggling APIC gets *less* traffic, not more.

## When to go async

- **Fan-out reads** — fabric audits, inventory collection, anything that
  aggregates many queries: `gather()` turns latency × N into latency × 1.
- **Services** — an event loop already runs; use the mirror, not threads.
- **One design, one push** — stay sync.  A single atomic POST gains nothing
  from an event loop, and the sync client reads better in a script.

Everything else on this page is the same as the sync client — including
{doc}`errors` and {doc}`connection`, which apply unchanged.
