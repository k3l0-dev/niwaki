"""The wave engine's fan-out bound, and the order it hands results back in.

Two properties that no test covered before, and that the engine had by accident
rather than by contract:

*Bound.* ``_run_waves`` is typed against the bare :class:`AsyncMoWriter`
protocol.  An :class:`AsyncApicSession` carries its own write semaphore, so a
push through the shipped façade was already capped — but that cap belonged to
the object that happened to be injected, not to the engine.  Against any other
conforming writer the whole wave went out at once.

*Order.* ``PushReport.dns`` is derived from the design, not from how fast the
controller answered.  That was true before and stayed true only because
``asyncio.gather`` happens to preserve argument order; the shape that replaces
it has to preserve it deliberately, and a plausible cheaper shape
(workers appending on completion) silently flips it.
"""

from __future__ import annotations

import asyncio

import pytest

from niwaki.design._engine import _Op, _run_waves
from niwaki.transport._protocols import AsyncMoWriter


class _CountingWriter:
    """A conforming ``AsyncMoWriter`` with no semaphore of its own.

    Deliberately not an ``AsyncApicSession``: the point is what the engine
    guarantees on its own, to any writer the protocol admits.
    """

    def __init__(self, delay: float = 0.01) -> None:
        self.delay = delay
        self.in_flight = 0
        self.peak_in_flight = 0
        self.peak_tasks = 0
        self.completion_order: list[str] = []

    async def post_mo(self, dn: str, payload: dict[str, object]) -> None:
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        self.peak_tasks = max(self.peak_tasks, len(asyncio.all_tasks()))
        await asyncio.sleep(self.delay)
        self.in_flight -= 1
        self.completion_order.append(dn)

    async def delete_mo(self, dn: str) -> None:  # pragma: no cover - unused here
        await self.post_mo(dn, {})


def _wave(width: int, depth_prefix: str = "uni/tn-t") -> list[_Op]:
    """*width* sibling ops at the same DN depth — i.e. exactly one wave."""
    return [_Op(dn=f"{depth_prefix}/BD-b{i:04d}", method="POST", payload={}) for i in range(width)]


class TestEngineBound:
    def test_a_conforming_writer_that_is_not_a_session_is_still_bounded(self) -> None:
        """The bound is the engine's promise, not a property of the injected object.

        This is the test that would have caught the accidental bound: before the
        pool, this stub saw all one hundred ops at once, because the only thing
        holding concurrency down was a semaphore living inside a class the
        engine never names.
        """
        writer = _CountingWriter()
        assert isinstance(writer, AsyncMoWriter), "the stub must satisfy the protocol"

        asyncio.run(_run_waves(writer, _wave(100), max_concurrent=10))

        assert writer.peak_in_flight <= 10
        assert writer.peak_in_flight == 10, "the pool should actually fill, not trickle"

    def test_coroutines_are_bounded_too_not_only_requests(self) -> None:
        """A semaphore under ``gather`` would bound requests and not coroutines.

        That distinction is the whole reason this lot exists: bounding requests
        was already done by the transport.  Measured, a semaphore-under-gather
        shape holds ~one task per op (101 for 100); a worker pool holds ~one per
        slot.  Without this assertion the two shapes are indistinguishable and
        the claim "the engine no longer builds a coroutine per op" is unfounded.
        """
        writer = _CountingWriter()
        asyncio.run(_run_waves(writer, _wave(100), max_concurrent=10))

        # The pool's workers, the runner, and pytest's own task: a small
        # constant, nowhere near one per op.
        assert writer.peak_tasks <= 10 + 5, f"{writer.peak_tasks} tasks for a pool of 10"

    def test_a_wave_narrower_than_the_pool_spawns_no_idle_workers(self) -> None:
        writer = _CountingWriter()
        asyncio.run(_run_waves(writer, _wave(3), max_concurrent=10))
        assert writer.peak_in_flight == 3

    def test_a_bound_of_one_serialises_the_wave(self) -> None:
        writer = _CountingWriter()
        asyncio.run(_run_waves(writer, _wave(8), max_concurrent=1))
        assert writer.peak_in_flight == 1

    def test_an_empty_op_list_is_not_an_error(self) -> None:
        outcome = asyncio.run(_run_waves(_CountingWriter(), []))
        assert (outcome.succeeded, outcome.failed, outcome.not_run) == ([], [], [])


class _ReversedLatencyWriter(_CountingWriter):
    """Answers the *last* op first, so submission and completion orders differ."""

    def __init__(self, width: int) -> None:
        super().__init__()
        self.width = width

    async def post_mo(self, dn: str, payload: dict[str, object]) -> None:
        index = int(dn.rsplit("b", 1)[1])
        await asyncio.sleep(0.001 * (self.width - index))
        self.completion_order.append(dn)


class TestSubmissionOrder:
    """Results follow the design, never the controller's answering speed."""

    def test_the_outcome_is_submission_order_not_completion_order(self) -> None:
        width = 12
        writer = _ReversedLatencyWriter(width)
        ops = _wave(width)

        outcome = asyncio.run(_run_waves(writer, ops, max_concurrent=width))

        submitted = [op.dn for op in ops]
        # Premise check: this run must genuinely have completed out of order,
        # otherwise the assertion below would pass on a technicality.
        assert writer.completion_order != submitted, "the two orders did not diverge"
        assert [op.dn for op in outcome.succeeded] == submitted

    def test_failures_are_recorded_in_submission_order_too(self) -> None:
        """``StagedPushError.failures`` inherits this order — it is public."""

        class _FailsTwoInReverse(_CountingWriter):
            async def post_mo(self, dn: str, payload: dict[str, object]) -> None:
                if dn.endswith("b0001"):
                    await asyncio.sleep(0.02)  # fails second, in time
                    raise RuntimeError("late")
                if dn.endswith("b0005"):
                    raise RuntimeError("early")  # fails first, in time
                await asyncio.sleep(0)

        outcome = asyncio.run(_run_waves(_FailsTwoInReverse(), _wave(8), max_concurrent=8))

        assert [op.dn for op, _ in outcome.failed] == [
            "uni/tn-t/BD-b0001",
            "uni/tn-t/BD-b0005",
        ]


class TestFailureSemanticsUnderThePool:
    def test_a_failure_isolates_its_subtree_and_never_its_siblings(self) -> None:
        """A wave wider than the pool must not change who is skipped.

        The skip decision has to stay in the sequential pre-pass; folding it
        into the workers would make ``not_run`` depend on scheduling.
        """

        class _FailsOneParent(_CountingWriter):
            async def post_mo(self, dn: str, payload: dict[str, object]) -> None:
                await asyncio.sleep(0.001)
                if dn == "uni/tn-t/BD-b0003":
                    raise RuntimeError("nope")

        parents = _wave(10)
        children = [
            _Op(dn=f"uni/tn-t/BD-b{i:04d}/subnet-[10.0.{i}.1/24]", method="POST", payload={})
            for i in range(10)
        ]
        outcome = asyncio.run(
            _run_waves(_FailsOneParent(), [*parents, *children], max_concurrent=3)
        )

        assert [op.dn for op, _ in outcome.failed] == ["uni/tn-t/BD-b0003"]
        assert [op.dn for op in outcome.not_run] == ["uni/tn-t/BD-b0003/subnet-[10.0.3.1/24]"]
        assert len(outcome.succeeded) == 18  # 9 parents + 9 untouched children

    @pytest.mark.parametrize("pool", [1, 2, 7, 100])
    def test_the_outcome_does_not_depend_on_the_pool_size(self, pool: int) -> None:
        class _FailsOne(_CountingWriter):
            async def post_mo(self, dn: str, payload: dict[str, object]) -> None:
                await asyncio.sleep(0.001)
                if dn.endswith("b0004"):
                    raise RuntimeError("nope")

        outcome = asyncio.run(_run_waves(_FailsOne(), _wave(9), max_concurrent=pool))
        assert [op.dn for op in outcome.succeeded] == [
            f"uni/tn-t/BD-b{i:04d}" for i in range(9) if i != 4
        ]
        assert [op.dn for op, _ in outcome.failed] == ["uni/tn-t/BD-b0004"]


class TestANonPositiveBoundIsRefused:
    """Silence is the one answer the engine must never give.

    A pool of zero runs no workers, leaves every slot empty, and hands back an
    outcome that is indistinguishable from a clean run of nothing: no failures,
    no skips, ``ok`` true.  Through ``push`` that surfaced as a ``PushReport``
    with an empty ``dns`` and ``request_count == 0``, no exception raised — a
    green deployment that wrote nothing.  Before the worker pool the same value
    made a client hang on its own semaphore, which at least was visible.
    """

    @pytest.mark.parametrize("bad", [0, -1, -50])
    def test_the_engine_refuses_it_rather_than_dropping_the_wave(self, bad: int) -> None:
        writer = _CountingWriter()
        with pytest.raises(ValueError, match="max_concurrent must be >= 1"):
            asyncio.run(_run_waves(writer, _wave(5), max_concurrent=bad))
        assert writer.peak_in_flight == 0  # and nothing was attempted

    def test_a_bound_of_one_is_still_legal(self) -> None:
        """The boundary is at zero, not at one — one is a serial push."""
        writer = _CountingWriter()
        outcome = asyncio.run(_run_waves(writer, _wave(4), max_concurrent=1))
        assert len(outcome.succeeded) == 4
