"""Design push engine — internal wave executor for ``staged`` mode.

Everything here is private to the design package: the
``_Op`` unit, the DN-depth toposort, and one wave engine shared by the sync
and async push paths.  Nothing in this module appears in public signatures or
results — ``push()`` reports plain DNs, and failures surface as
:exc:`~niwaki.exceptions.StagedPushError`.

Why DN depth works as an ordering key: ACI DNs encode the full object
hierarchy — ``uni/tn-prod/BD-web`` always depends on ``uni/tn-prod``.  Ops at
the same depth are independent; the async engine runs each wave concurrently.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from niwaki._logging import wave_started
from niwaki.transport._protocols import AsyncMoWriter

# Default ops in flight within one wave when :func:`_run_waves` is driven
# directly.  Mirrors the transport's own write bound so a bare writer behaves
# like a real session; the push path passes the client's limit instead, so this
# constant is never what a user's push is throttled to.  Precedent for the shape
# and the naming: ``_VERIFY_CONCURRENCY`` in ``_verify.py``.
_WAVE_CONCURRENCY = 10


@dataclass(frozen=True)
class _Op:
    """One write operation: what (method), where (DN), and which payload."""

    dn: str
    method: Literal["POST", "DELETE"]
    payload: dict[str, Any] | None = field(default=None)

    @property
    def depth(self) -> int:
        """DN segment count minus one, bracket-aware.

        Slashes inside bracketed naming values are not segment separators:
        ``uni/tn-p/BD-w/subnet-[10.0.1.1/24]`` is depth 3, and nested brackets
        (``rspathAtt-[topology/pod-1/paths-101/pathep-[eth1/1]]``) count as
        one segment too.
        """
        depth = 0
        bracket_level = 0
        for char in self.dn:
            if char == "[":
                bracket_level += 1
            elif char == "]":
                bracket_level -= 1
            elif char == "/" and bracket_level == 0:
                depth += 1
        return depth


@dataclass
class _WaveOutcome:
    """Bookkeeping of one engine run — never exported.

    Attributes:
        succeeded: Ops that completed, in submission order — the order the
            wave presented them, not the order they finished in.
        failed: ``(op, exception)`` pairs for ops that raised.
        not_run: Ops skipped because an *ancestor* op failed — pushing a child
            whose parent never landed would only 404.  Independent branches are
            never in here: a failure isolates its own subtree, not its siblings.
    """

    succeeded: list[_Op]
    failed: list[tuple[_Op, Exception]]
    not_run: list[_Op]

    @property
    def ok(self) -> bool:
        return not self.failed and not self.not_run


def _toposort(ops: Sequence[_Op]) -> list[list[_Op]]:
    """Group ops into waves by ascending DN depth (parents before children)."""
    if not ops:
        return []
    by_depth: dict[int, list[_Op]] = {}
    for op in ops:
        by_depth.setdefault(op.depth, []).append(op)
    return [by_depth[d] for d in sorted(by_depth)]


def _descends_from_failed(dn: str, failed_dns: set[str]) -> bool:
    """Whether *dn* is at or below a DN that already failed this run.

    DN ancestry is a clean segment-prefix test: ``uni/tn-p/BD-web/subnet-[..]``
    descends from ``uni/tn-p/BD-web`` (prefix followed by ``/``), while the
    sibling ``uni/tn-p/BD-web2`` does not — the separating slash keeps
    ``BD-web`` from matching ``BD-web2``.
    """
    return any(dn == failed or dn.startswith(f"{failed}/") for failed in failed_dns)


def _run_waves_sync(execute: Callable[[_Op], None], ops: Sequence[_Op]) -> _WaveOutcome:
    """Run *ops* in DN-depth waves, one at a time, through *execute*.

    A failure isolates only its own subtree: descendants of a failed op are
    recorded as ``not_run`` (they would 404 without their parent), while every
    independent branch runs to completion.  Same-depth ops in a wave are never
    ancestors of one another, so a failure never skips a sibling.
    """
    outcome = _WaveOutcome(succeeded=[], failed=[], not_run=[])
    failed_dns: set[str] = set()
    for wave in _toposort(ops):
        wave_started(wave[0].depth, len(wave), 1)
        for op in wave:
            if _descends_from_failed(op.dn, failed_dns):
                outcome.not_run.append(op)
                continue
            try:
                execute(op)
                outcome.succeeded.append(op)
            except Exception as exc:
                outcome.failed.append((op, exc))
                failed_dns.add(op.dn)
    return outcome


async def _run_waves(
    session: AsyncMoWriter,
    ops: Sequence[_Op],
    *,
    max_concurrent: int = _WAVE_CONCURRENCY,
) -> _WaveOutcome:
    """Run *ops* in DN-depth waves; ops within a wave run concurrently, bounded.

    Same toposort and same subtree-isolated failure semantics as
    :func:`_run_waves_sync` — only the intra-wave execution differs.  The skip
    decision reads the previous waves' failures, so partitioning a wave before
    running it is safe: same-depth ops never descend from one another.

    A fixed pool of workers pulls from the wave rather than one coroutine being
    created per op.  That matters because this function is typed against the
    bare :class:`AsyncMoWriter` protocol: an :class:`AsyncApicSession` carries
    its own write semaphore, but any other conforming writer does not, and
    inheriting a bound from whichever object was injected is an accident rather
    than a contract.  Bounding here makes it the engine's own promise.  It also
    stops the engine materialising one coroutine and one retained payload per
    op — a 10 000-op wave costs ~0.8 MB this way against ~15 MB for a bare
    ``gather``.

    Results land in **submission order**, not completion order: each worker
    writes into the slot its op came from.  That is what keeps
    ``PushReport.dns`` derived from the design rather than from how fast the
    controller happened to answer.

    Args:
        session: Anything satisfying :class:`AsyncMoWriter`.
        ops: The operations to run, in any order — the toposort re-orders them.
        max_concurrent: Upper bound on ops in flight within one wave.  The
            caller passes the effective bound; the default is only reached when
            this function is driven directly.

    Returns:
        A :class:`_WaveOutcome` — succeeded, failed and never-attempted ops.
    """

    async def _run(op: _Op) -> tuple[_Op, Exception | None]:
        # Converting the failure into a return value is load-bearing: it is why
        # one op failing never cancels its siblings.  Letting ops raise into a
        # TaskGroup instead would cancel the whole wave.
        try:
            if op.method == "POST":
                await session.post_mo(op.dn, op.payload or {})
            else:
                await session.delete_mo(op.dn)
            return op, None
        except Exception as exc:
            return op, exc

    if max_concurrent < 1:
        # A pool of zero would run no workers, leave every slot empty, and hand
        # back an outcome that looks like a clean, complete run of nothing.
        # Silence is the one answer this function must never give.
        raise ValueError(f"max_concurrent must be >= 1, got {max_concurrent}")

    outcome = _WaveOutcome(succeeded=[], failed=[], not_run=[])
    failed_dns: set[str] = set()
    for wave in _toposort(ops):
        wave_started(wave[0].depth, len(wave), min(max_concurrent, len(wave)))
        to_run: list[_Op] = []
        for op in wave:
            if _descends_from_failed(op.dn, failed_dns):
                outcome.not_run.append(op)
            else:
                to_run.append(op)

        slots: list[tuple[_Op, Exception | None] | None] = [None] * len(to_run)
        # One shared iterator, pulled by every worker.  Safe because ``next()``
        # contains no ``await``: asyncio is single-threaded, so the pull is
        # atomic by construction.  That invariant dies if the source ever
        # becomes an async generator.
        pending = enumerate(to_run)

        # Both closed-over names are bound as defaults: the loop rebinds them
        # each wave, and a late-bound closure would be a real bug here even
        # though the gather below happens to complete first.
        async def _worker(
            pending: enumerate[_Op] = pending,
            slots: list[tuple[_Op, Exception | None] | None] = slots,
        ) -> None:
            for index, op in pending:
                slots[index] = await _run(op)

        await asyncio.gather(*(_worker() for _ in range(min(max_concurrent, len(to_run)))))

        for op, exc in (slot for slot in slots if slot is not None):
            if exc is None:
                outcome.succeeded.append(op)
            else:
                outcome.failed.append((op, exc))
                failed_dns.add(op.dn)
    return outcome
