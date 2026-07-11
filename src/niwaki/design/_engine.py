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

from niwaki.transport._protocols import AsyncMoWriter


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
        succeeded: Ops that completed, in execution order.
        failed: ``(op, exception)`` pairs for ops that raised.
        not_run: Ops skipped because an earlier wave failed.
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


def _account_waves(
    waves: list[list[_Op]],
    wave_results: list[list[tuple[_Op, Exception | None]]],
    *,
    continue_on_failure: bool,
) -> _WaveOutcome:
    """Fold per-wave results into one outcome (shared sync/async accounting).

    ``wave_results`` may be shorter than ``waves`` when the caller stopped
    early — every op of the unexecuted waves is recorded as ``not_run``.
    """
    outcome = _WaveOutcome(succeeded=[], failed=[], not_run=[])
    for results in wave_results:
        for op, exc in results:
            if exc is None:
                outcome.succeeded.append(op)
            else:
                outcome.failed.append((op, exc))
    if outcome.failed and not continue_on_failure:
        executed = len(wave_results)
        for remaining in waves[executed:]:
            outcome.not_run.extend(remaining)
    return outcome


def _run_waves_sync(
    execute: Callable[[_Op], None],
    ops: Sequence[_Op],
    *,
    continue_on_failure: bool = False,
) -> _WaveOutcome:
    """Run *ops* in DN-depth waves, one at a time, through *execute*.

    A failing wave stops the remaining ones unless *continue_on_failure*.
    """
    waves = _toposort(ops)
    wave_results: list[list[tuple[_Op, Exception | None]]] = []
    for wave in waves:
        results: list[tuple[_Op, Exception | None]] = []
        for op in wave:
            try:
                execute(op)
                results.append((op, None))
            except Exception as exc:
                results.append((op, exc))
        wave_results.append(results)
        if any(exc is not None for _, exc in results) and not continue_on_failure:
            break
    return _account_waves(waves, wave_results, continue_on_failure=continue_on_failure)


async def _run_waves(
    session: AsyncMoWriter,
    ops: Sequence[_Op],
    *,
    continue_on_failure: bool = False,
) -> _WaveOutcome:
    """Run *ops* in DN-depth waves; ops within a wave run concurrently.

    Same toposort, same failure semantics, same accounting as
    :func:`_run_waves_sync` — only the intra-wave execution differs
    (``asyncio.gather`` against an :class:`AsyncMoWriter`).
    """

    async def _run(op: _Op) -> tuple[_Op, Exception | None]:
        try:
            if op.method == "POST":
                await session.post_mo(op.dn, op.payload or {})
            else:
                await session.delete_mo(op.dn)
            return op, None
        except Exception as exc:
            return op, exc

    waves = _toposort(ops)
    wave_results: list[list[tuple[_Op, Exception | None]]] = []
    for wave in waves:
        results = list(await asyncio.gather(*[_run(op) for op in wave]))
        wave_results.append(results)
        if any(exc is not None for _, exc in results) and not continue_on_failure:
            break
    return _account_waves(waves, wave_results, continue_on_failure=continue_on_failure)
