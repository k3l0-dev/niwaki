"""Push execution — strict / staged / plan, sync and async.

The heavy lifting (validation, resolution, compilation) is pure and shared;
this module only adds the result types and the thin I/O wrappers around the
existing engine:

- ``strict`` → one atomic POST of the ``polUni`` envelope to ``/api/mo/uni.json``
  (all-or-nothing on the APIC side);
- ``staged`` → per-object ops executed by the wave engine
  (:mod:`niwaki.design._engine`: waves by DN depth, parents before children;
  atomic classes ship their subtree whole); a partial failure raises
  :exc:`~niwaki.exceptions.StagedPushError` carrying plain DNs;
- ``plan`` → read the current state and diff it against the desired tree via
  :func:`niwaki.utils.diff.mo_diff` — nothing is pushed.  One read per
  declared domain (direct child of ``polUni``), **scoped with
  ``rsp-subtree-class`` to the classes the design declares** (R-3): an
  unscoped full read of ``uni/fabric`` exceeds the APIC query limit.

Per the owner's decision, the engine's op unit never appears in the public
result types — reports and errors carry plain DN strings.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from niwaki.design._compiler import build_desired_tree, compile_ops, compile_poluni
from niwaki.design._engine import _Op, _run_waves, _run_waves_sync, _WaveOutcome
from niwaki.design._node import DesignNode
from niwaki.design._resolver import resolve
from niwaki.exceptions._design import StagedPushError
from niwaki.models.base import ManagedObject
from niwaki.utils.diff import mo_diff

if TYPE_CHECKING:
    from niwaki.design._cursor import PushMode
    from niwaki.facade import AsyncNiwaki, Niwaki


@dataclass(frozen=True)
class PushReport:
    """Summary of a successful ``strict`` or ``staged`` push.

    Attributes:
        mode: The push mode that produced this report.
        dns: DNs written, in execution order (includes resolved Rs objects).
        request_count: Number of HTTP requests issued (1 for ``strict``).
    """

    mode: str
    dns: list[str]
    request_count: int


@dataclass(frozen=True)
class PlanResult:
    """Dry-run report of what a push would change (``plan`` mode).

    Deletions are out of scope by design: a plan never proposes removing
    objects that exist on the APIC but not in the design.

    Attributes:
        creates: DNs that do not exist on the APIC and would be created.
        updates: Per-DN field changes as ``{field: (current, desired)}``.
        unchanged: DNs already matching the desired state.
    """

    creates: list[str]
    updates: dict[str, dict[str, tuple[Any, Any]]]
    unchanged: list[str]

    @property
    def has_changes(self) -> bool:
        """``True`` when a push would modify anything on the APIC."""
        return bool(self.creates or self.updates)


# ── Pure helpers ──────────────────────────────────────────────────────────────


def build_payload(root: DesignNode) -> dict[str, Any]:
    """Resolve the design and return the atomic ``polUni`` push payload."""
    return compile_poluni(root, resolve(root))


def _walk_dns(root: DesignNode, extras: dict[DesignNode, list[ManagedObject]]) -> list[str]:
    """List every DN the design covers, in parents-first order.

    The polUni root is not listed — it always exists on the APIC.
    """
    dns: list[str] = []

    def _walk(node: DesignNode, parent_dn: str) -> None:
        dn = f"{parent_dn}/{node.rn}"
        dns.append(dn)
        dns.extend(f"{dn}/{rs.rn}" for rs in extras.get(node, []))
        for child in node.children:
            _walk(child, dn)

    for child in root.children:
        _walk(child, "uni")
    return dns


def _plan_result(
    desired: ManagedObject,
    current: ManagedObject | None,
    root_dn: str,
) -> PlanResult:
    """Diff the desired tree against the current APIC tree (pure)."""
    creates: list[str] = []
    updates: dict[str, dict[str, tuple[Any, Any]]] = {}
    unchanged: list[str] = []

    def _walk(d: ManagedObject, c: ManagedObject | None, dn: str) -> None:
        if c is None or type(c) is not type(d):
            creates.append(dn)
            for child in d.children:
                _walk(child, None, f"{dn}/{child.rn}")
            return

        delta = mo_diff(d, c, recurse_children=False, respect_fields_set=True)
        if delta is None:
            unchanged.append(dn)
        else:
            naming = set(type(d)._naming_props)  # pyright: ignore[reportPrivateUsage]
            fields = sorted(delta.model_fields_set - naming - {"children"})
            updates[dn] = {f: (getattr(c, f, None), getattr(d, f, None)) for f in fields}

        current_children = {(type(child), child.rn): child for child in c.children}
        for child in d.children:
            _walk(child, current_children.get((type(child), child.rn)), f"{dn}/{child.rn}")

    _walk(desired, current, root_dn)
    return PlanResult(creates=creates, updates=updates, unchanged=unchanged)


def _merge_plans(parts: list[PlanResult]) -> PlanResult:
    """Aggregate per-domain plan results into one report."""
    return PlanResult(
        creates=[dn for part in parts for dn in part.creates],
        updates={dn: fields for part in parts for dn, fields in part.updates.items()},
        unchanged=[dn for part in parts for dn in part.unchanged],
    )


def _plan_read_params(desired: ManagedObject) -> dict[str, str]:
    """Query parameters for one plan read, scoped to the design's classes.

    An unscoped ``rsp-subtree=full`` on ``uni/fabric`` or ``uni/infra`` blows
    the APIC query limit ("result dataset is too big", HTTP 400) — R-3.
    Restricting the subtree to the classes the design actually declares keeps
    the read small and the diff exact: every intermediate node of the desired
    tree contributes its class, so the returned hierarchy stays connected,
    and foreign instances of the same classes are ignored by the
    ``(class, rn)`` matcher.
    """
    classes: set[str] = set()

    def _collect(mo: ManagedObject) -> None:
        classes.add(mo._aci_class)  # pyright: ignore[reportPrivateUsage]
        for child in mo.children:
            _collect(child)

    _collect(desired)
    return {"rsp-subtree": "full", "rsp-subtree-class": ",".join(sorted(classes))}


def _staged_report(ops: list[_Op], outcome: _WaveOutcome) -> PushReport:
    """Turn an engine outcome into the public report, or raise on failure.

    Raises:
        StagedPushError: At least one operation failed; the exception carries
            the partial report plus the failed and skipped DNs.
    """
    report = PushReport(
        mode="staged",
        dns=[op.dn for op in outcome.succeeded],
        request_count=len(ops),
    )
    if not outcome.ok:
        raise StagedPushError(
            report,
            failures=[(op.dn, exc) for op, exc in outcome.failed],
            not_run=[op.dn for op in outcome.not_run],
        )
    return report


# ── Sync execution ────────────────────────────────────────────────────────────


def push_sync(root: DesignNode, client: Niwaki, mode: PushMode) -> PushReport | PlanResult:
    """Execute a push through a sync :class:`~niwaki.facade.Niwaki` client.

    See :meth:`niwaki.design.Cursor.push` for the full mode contract.
    """
    extras = resolve(root)
    session = client._sync_session  # pyright: ignore[reportPrivateUsage]

    if mode == "strict":
        session.post_mo("uni", compile_poluni(root, extras))
        return PushReport(mode="strict", dns=_walk_dns(root, extras), request_count=1)

    if mode == "staged":
        ops = compile_ops(root, extras)

        def _execute(op: _Op) -> None:
            if op.method == "POST":
                session.post_mo(op.dn, op.payload or {})
            else:
                session.delete_mo(op.dn)

        return _staged_report(ops, _run_waves_sync(_execute, ops))

    # plan: one read + diff per direct child of polUni (per declared domain),
    # scoped to the design's classes (R-3).
    parts: list[PlanResult] = []
    for child in root.children:
        desired = build_desired_tree(child, extras)
        child_dn = f"uni/{desired.rn}"
        raw = session.get(f"/api/mo/{child_dn}.json", **_plan_read_params(desired))
        current = ManagedObject.from_apic(raw[0]) if raw else None
        parts.append(_plan_result(desired, current, child_dn))
    return _merge_plans(parts)


# ── Async execution ───────────────────────────────────────────────────────────


async def push_async(
    root: DesignNode,
    client: AsyncNiwaki,
    mode: PushMode,
) -> PushReport | PlanResult:
    """Execute a push through an :class:`~niwaki.facade.AsyncNiwaki` client.

    Mirror of :func:`push_sync` — validation, resolution, and compilation are
    the same pure code; only the three I/O calls are awaited.
    """
    extras = resolve(root)
    session = client._active_session  # pyright: ignore[reportPrivateUsage]

    if mode == "strict":
        await session.post_mo("uni", compile_poluni(root, extras))
        return PushReport(mode="strict", dns=_walk_dns(root, extras), request_count=1)

    if mode == "staged":
        ops = compile_ops(root, extras)
        return _staged_report(ops, await _run_waves(session, ops))

    # plan: one read + diff per direct child of polUni (per declared domain),
    # scoped to the design's classes (R-3).
    parts: list[PlanResult] = []
    for child in root.children:
        desired = build_desired_tree(child, extras)
        child_dn = f"uni/{desired.rn}"
        raw = await session.get(f"/api/mo/{child_dn}.json", **_plan_read_params(desired))
        current = ManagedObject.from_apic(raw[0]) if raw else None
        parts.append(_plan_result(desired, current, child_dn))
    return _merge_plans(parts)
