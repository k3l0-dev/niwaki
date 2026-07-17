# Async at scale

**Problem** — `commerce` was the first tenant; now there are dozens of lines of
business to provision and audit on the same fabric.  Doing that one blocking
request at a time wastes wall-clock time on network latency.  `AsyncNiwaki` is a
strict mirror of the sync client — same designs, same query builder, same
`push()` — and `gather()` turns *latency × N* into *latency × 1*.

The rule of thumb: **one design, one push → stay sync**; **many independent
operations → go async**.

## Designs are transport-agnostic

A design does not know or care whether it is pushed sync or async — transport is
injected at `push()` time.  The same `build()` you would call in a script is
awaitable through the async client:

```python
import asyncio

from niwaki import AsyncNiwaki
from niwaki.design import tenant


def build(name: str) -> object:
    config = tenant(name)
    config.vrf("prod")
    config.bd("bd-web", unicast_routing=True).bind(vrf="prod").subnet("10.30.10.1/24")
    return config


async def onboard_one() -> None:
    async with AsyncNiwaki("https://apic.example.com", "admin", "secret") as aci:
        await build("commerce").push(aci)
        bd = await aci.tenant("commerce").bd("bd-web").read()
        assert bd.unicast_routing is True


asyncio.run(onboard_one())
```

## Fan out with `gather()`

`gather()` runs several awaitables under one `asyncio.TaskGroup` and returns
their results in order.  Provisioning a batch of tenants is one call — each is
its own atomic push, all in flight together:

```python
async def onboard_batch(names: list[str]) -> set[str]:
    async with AsyncNiwaki("https://apic.example.com", "admin", "secret") as aci:
        await aci.gather(*(build(name).push(aci) for name in names))
        tenants = await aci.query("fvTenant").fetch()
        return {t.name for t in tenants}


provisioned = asyncio.run(onboard_batch(["shop-eu", "shop-us", "shop-apac"]))
assert {"shop-eu", "shop-us", "shop-apac"} <= provisioned
```

Two properties come from the TaskGroup: **structured concurrency** (when
`gather()` returns, every task is finished — nothing leaks) and **grouped
failures** (if several coroutines fail, all failures collect into an
`ExceptionGroup` rather than the first masking the rest).

## Audit the whole batch at once

The read side fans out the same way — an inventory of every tenant in one
latency:

```python
async def audit() -> dict[str, int]:
    async with AsyncNiwaki("https://apic.example.com", "admin", "secret") as aci:
        tenants, bds, faults = await aci.gather(
            aci.query("fvTenant").fetch(),
            aci.query("fvBD").fetch(),
            aci.query("faultInst").fetch(),
        )
        return {"tenants": len(tenants), "bds": len(bds), "faults": len(faults)}


print(asyncio.run(audit()))
```

## Handling partial failure

When one operation in a fan-out fails, catch the group and inspect each failure —
the rest still ran:

```python
from niwaki.exceptions import NiwakiError


async def audit_resilient() -> None:
    async with AsyncNiwaki("https://apic.example.com", "admin", "secret") as aci:
        try:
            await aci.gather(
                aci.query("fvTenant").fetch(),
                aci.query("topSystem").fetch(),
            )
        except* NiwakiError as group:
            for exc in group.exceptions:
                print("failed:", exc)


asyncio.run(audit_resilient())
```

## Variations & pitfalls

- **Concurrency is bounded** — a session-level semaphore (`max_concurrent`,
  default 10) caps in-flight requests however wide you fan out; raise it with
  `AsyncNiwaki(..., max_concurrent=20)` only for a fabric that welcomes the load.
- **Retries compose with the limit** — a retrying request keeps its slot, so a
  struggling APIC gets *less* traffic, not more.
- **Token refreshes are serialised** — a hundred concurrent reads never race a
  re-login; there is nothing to coordinate by hand ({doc}`../guide/connection`).
- **Stay sync for a single atomic push** — one `strict` POST gains nothing from
  an event loop, and a sync script reads more plainly.
