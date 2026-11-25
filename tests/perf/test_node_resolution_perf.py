"""``node(dn)`` resolves its class by walking the DN — repeated calls must be amortised.

Since 2.2, ``aci.node(dn)`` walks the DN from ``uni`` against the containment
tables to learn the class (and, with a stated class, to check it).  Measured
uncached: about 1.3 ms per resolvable DN and 2-8 ms per unresolvable or
ambiguous one (the DN-grammar fallback scans the catalogue) — against about
2 µs for the pre-2.2 plain constructor.  A local loop such as
``[aci.node(mo.dn) for mo in query.fetch()]`` over 10 000 objects then burns
seconds of CPU.

The contract: the per-segment child-class resolution is memoised in a
**bounded** cache, so a repeated segment costs a lookup — resolvable,
unresolvable and ambiguous alike (the outcome of a segment is a pure function
of ``(parent class, segment)``).

The budgets are calibrated, not tight: 2 500 calls over a handful of DNs took
1.9 s uncached and 4-8 ms with a per-segment ``lru_cache`` on the same
machine, so 0.25 s fails today by ~7x and passes with a cache by ~30x — a
loaded runner stays green, a missing cache does not.  Each guard keeps the
fastest of a few repetitions (the least perturbed by the scheduler).

The boundedness check reads the caches through :mod:`functools`' public
introspection (``cache_info``/``cache_parameters``) on the resolver modules
(``niwaki._containment``, ``niwaki.facade``): the memo must be a
``functools.lru_cache`` with a finite ``maxsize``, and no unbounded cache may
grow with the number of distinct DNs.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from types import ModuleType
from typing import Protocol

import pytest

from niwaki import AsyncNiwaki, Niwaki
from niwaki.models._generated.fv.fvBD import fvBD

pytestmark = pytest.mark.perf

_BUDGET_S = 0.25

_RESOLVABLE = (
    "uni/tn-prod/BD-web",
    "uni/tn-prod/ap-shop/epg-web",
    "uni/tn-prod/BD-web/subnet-[10.0.0.1/24]",
    "uni/tn-prod/ap-shop/epg-web/rspathAtt-[topology/pod-1/paths-101/pathep-[eth1/1]]",
    "uni/infra/nprof-leaf101/leaves-s1-typ-range",
    "uni/tn-prod/ctx-vrf",
    "uni/vmmp-VMware/dom-dc1",
    "uni/tn-x/fault-F0123",  # resolves to a catalogue-only class
)

_UNRESOLVED = (
    "uni/tn-x/nope-1",  # unknown segment: the DN-grammar scan finds nothing
    "uni/infra/nodecfgcont/node-101",  # ambiguous: infraNodeCfg vs fabricNodeCfg
    "uni/tn-x/acIpToIp-y/acl3outcont/outdef-z/lnodep-n/lifp-i",  # ambiguous, generated
)


def _clients() -> tuple[Niwaki, AsyncNiwaki]:
    return (
        Niwaki("https://apic.invalid", "admin", "secret"),
        AsyncNiwaki("https://apic.invalid", "admin", "secret"),
    )


def _fastest(run: Callable[[], None], *, repeats: int) -> float:
    run()  # warm-up: imports, catalogue opening, first fills of the cache
    best = float("inf")
    for _ in range(repeats):
        start = time.perf_counter()
        run()
        best = min(best, time.perf_counter() - start)
    return best


def test_repeated_node_calls_are_amortised() -> None:
    """2 000 class-less ``node(dn)`` calls + 500 with a stated class, under budget."""
    sync, async_ = _clients()

    def run() -> None:
        for i in range(2_000):
            client = sync if i % 2 else async_
            client.node(_RESOLVABLE[i % len(_RESOLVABLE)])
        for _ in range(500):
            sync.node("uni/tn-prod/BD-web", fvBD)

    elapsed = _fastest(run, repeats=3)
    assert elapsed < _BUDGET_S, (
        f"2 500 node() calls over {len(_RESOLVABLE)} DNs took {elapsed:.3f} s "
        f"(budget {_BUDGET_S} s) — is the per-segment resolution memoised?"
    )


def test_repeated_unresolvable_and_ambiguous_dns_are_amortised() -> None:
    """A segment that resolves to nothing, or to several classes, is memoised too."""
    sync, async_ = _clients()

    def run() -> None:
        for i in range(450):
            client = sync if i % 2 else async_
            client.node(_UNRESOLVED[i % len(_UNRESOLVED)])

    elapsed = _fastest(run, repeats=2)
    assert elapsed < _BUDGET_S, (
        f"450 node() calls over unresolvable/ambiguous DNs took {elapsed:.3f} s "
        f"(budget {_BUDGET_S} s) — only successful resolutions seem to be memoised"
    )


class _CacheInfo(Protocol):
    hits: int
    currsize: int


class _Cached(Protocol):
    def cache_info(self) -> _CacheInfo: ...

    def cache_parameters(self) -> dict[str, object]: ...


def _caches() -> dict[str, _Cached]:
    """Every ``functools`` cache defined at module level in the resolver modules."""
    import niwaki._containment
    import niwaki.facade

    modules: tuple[ModuleType, ...] = (niwaki._containment, niwaki.facade)
    found: dict[str, _Cached] = {}
    for module in modules:
        for name, value in vars(module).items():
            if callable(getattr(value, "cache_info", None)) and callable(
                getattr(value, "cache_parameters", None)
            ):
                found[f"{module.__name__}.{name}"] = value
    return found


def test_the_resolution_cache_is_bounded() -> None:
    """A bounded ``lru_cache`` serves repeated segments; nothing grows per distinct DN."""
    sync, async_ = _clients()
    for _ in range(3):
        for dn in _RESOLVABLE:
            sync.node(dn)
            async_.node(dn)
    caches = _caches()
    bounded = {
        name: cache
        for name, cache in caches.items()
        if cache.cache_parameters().get("maxsize") is not None
    }
    assert bounded, (
        "no bounded functools.lru_cache in niwaki._containment / niwaki.facade — "
        f"caches found: {sorted(caches)}"
    )
    assert sum(cache.cache_info().hits for cache in bounded.values()) > 0, (
        f"the bounded caches {sorted(bounded)} served no repeated node() call"
    )

    before = {name: cache.cache_info().currsize for name, cache in caches.items()}
    distinct = 300
    for i in range(distinct):
        sync.node(f"uni/tn-t{i}/BD-b{i}")
    for name, cache in caches.items():
        size = cache.cache_info().currsize
        maxsize = cache.cache_parameters().get("maxsize")
        if maxsize is None:
            assert size - before[name] < distinct, (
                f"{name} is unbounded and grew by one entry per distinct DN"
            )
        else:
            assert isinstance(maxsize, int)
            assert size <= maxsize
