"""Latency budgets for the read catalogue's property lookups — gates, not benchmarks.

Normalising the ``prop`` table into shared signatures (``prop_sig``) plus
memberships (``class_prop``) trades a join for about 9.7 MB of installed
weight.  The join is free on the per-class path and costs something on the
schema-wide scans; these budgets bound that cost:

=================================  ==========  ==============================
Lookup                             Budget      Measured before normalisation
=================================  ==========  ==============================
``find_prop("mac")``               150 ms      ~8 ms (LIMIT 50 walks the key)
``_secure_props()`` (schema-wide)  300 ms      ~230 ms idle, ~350 ms loaded
``class_meta("fvBD")``, cold       5 ms        ~0.3 ms
=================================  ==========  ==============================

``find_prop`` was measured at about 90 ms once rewritten to filter the
signatures first (a view over the join took 308 ms — hence the rewrite).
``_secure_props`` is expected to *drop*: the ``secure`` flag can be tested
on 25,901 signatures instead of 332,297 rows.

**What "cold" means.**  ``class_meta`` is memoised per class, and the first
lookup on a reader also loads the label and type pools (~1 MB, ~15 ms) —
a one-off per process that the normalisation does not touch.  The budget is
on what the normalisation *does* touch: the first ``class_meta("fvBD")`` on a
fresh reader whose pools are already loaded (by another class).

**Robust timing.**  Each sample uses a fresh :class:`Catalog`, so no memo
answers for it; the budget applies to the *minimum* of several samples — the
one least perturbed by scheduler noise — so a loaded machine or a slower CI
runner does not flake the gate, while an order-of-change regression (a scan
where a seek belongs) still trips it.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from niwaki.query import _catalog

_FIND_PROP_BUDGET_MS = 150.0
_SECURE_PROPS_BUDGET_MS = 300.0
_CLASS_META_COLD_BUDGET_MS = 5.0


def _best_ms(sample: Callable[[], float], *, runs: int) -> float:
    """The fastest of ``runs`` samples, each returning its own elapsed seconds."""
    return min(sample() for _ in range(runs)) * 1_000


def _fresh() -> _catalog.Catalog:
    """A reader on the shipped db with its connection and flag layout open.

    Opening the file and reading the manifest is the same before and after
    the normalisation, so it stays outside every timed region.
    """
    cat = _catalog.Catalog(_catalog.DEFAULT_PATH)
    cat._flags_order()  # pyright: ignore[reportPrivateUsage]
    return cat


@pytest.mark.perf
def test_find_prop_under_budget() -> None:
    def sample() -> float:
        cat = _fresh()
        try:
            start = time.perf_counter()
            found = cat.find_prop("mac")
            elapsed = time.perf_counter() - start
        finally:
            cat.close()
        assert len(found) == 50  # the default limit, filled: a real answer was timed
        return elapsed

    measured = _best_ms(sample, runs=5)
    assert measured <= _FIND_PROP_BUDGET_MS, (
        f'find_prop("mac") took {measured:.1f} ms, over the {_FIND_PROP_BUDGET_MS:.0f} ms '
        "budget. Filter the signatures first, then join the memberships."
    )


@pytest.mark.perf
def test_secure_props_under_budget() -> None:
    def sample() -> float:
        cat = _fresh()
        try:
            start = time.perf_counter()
            found = cat._secure_props()  # pyright: ignore[reportPrivateUsage]
            elapsed = time.perf_counter() - start
        finally:
            cat.close()
        assert ("aaaUser", "pwd") in found
        return elapsed

    measured = _best_ms(sample, runs=3)
    assert measured <= _SECURE_PROPS_BUDGET_MS, (
        f"_secure_props() took {measured:.1f} ms, over the {_SECURE_PROPS_BUDGET_MS:.0f} ms "
        "budget. Test the secure flag on the signatures before joining classes."
    )


@pytest.mark.perf
def test_class_meta_cold_under_budget() -> None:
    def sample() -> float:
        cat = _fresh()
        try:
            cat.class_meta("fvCtx")  # loads the label/type pools, not fvBD
            start = time.perf_counter()
            meta = cat.class_meta("fvBD")
            elapsed = time.perf_counter() - start
        finally:
            cat.close()
        assert meta.readable_to_wire["arp_flooding"] == "arpFlood"
        return elapsed

    measured = _best_ms(sample, runs=5)
    assert measured <= _CLASS_META_COLD_BUDGET_MS, (
        f'class_meta("fvBD") on a fresh reader took {measured:.2f} ms, over the '
        f"{_CLASS_META_COLD_BUDGET_MS:.0f} ms budget. A per-class lookup must seek "
        "the class's memberships, not scan the signatures."
    )
