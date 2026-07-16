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


async def _run_waves(session: AsyncMoWriter, ops: Sequence[_Op]) -> _WaveOutcome:
    """Run *ops* in DN-depth waves; ops within a wave run concurrently.

    Same toposort and same subtree-isolated failure semantics as
    :func:`_run_waves_sync` — only the intra-wave execution differs
    (``asyncio.gather`` against an :class:`AsyncMoWriter`).  The skip decision
    reads the previous waves' failures, so partitioning a wave before gathering
    it is safe: same-depth ops never descend from one another.
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

    outcome = _WaveOutcome(succeeded=[], failed=[], not_run=[])
    failed_dns: set[str] = set()
    for wave in _toposort(ops):
        to_run: list[_Op] = []
        for op in wave:
            if _descends_from_failed(op.dn, failed_dns):
                outcome.not_run.append(op)
            else:
                to_run.append(op)
        for op, exc in await asyncio.gather(*[_run(op) for op in to_run]):
            if exc is None:
                outcome.succeeded.append(op)
            else:
                outcome.failed.append((op, exc))
                failed_dns.add(op.dn)
    return outcome
