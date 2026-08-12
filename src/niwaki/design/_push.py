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
  :func:`niwaki.utils.diff.mo_diff` — nothing is pushed.  One **sharded flat
  read** per declared domain (direct child of ``polUni``), scoped to the
  classes the design declares (R-3, both ends: an unscoped full read of
  ``uni/fabric`` exceeds the APIC result limit, and a large design's class
  list in a single query string exceeds the request-line limit).  The tree is
  rebuilt from DNs by :mod:`niwaki._read`.

Per the owner's decision, the engine's op unit never appears in the public
result types — reports and errors carry plain DN strings.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from niwaki._logging import operation_failed, push_finished, push_started
from niwaki._read import read_subtree, read_subtree_async
from niwaki.design._compiler import build_desired_tree, compile_ops, compile_poluni
from niwaki.design._engine import _Op, _run_waves, _run_waves_sync, _WaveOutcome
from niwaki.design._node import DesignNode
from niwaki.design._resolver import resolve
from niwaki.design._verify import (
    _VERIFY_CONCURRENCY,
    RefCheck,
    collect_external_refs,
    failures_of,
    verify_async,
    verify_sync,
)
from niwaki.exceptions._design import DanglingReferenceError, StagedPushError
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
        dns: The DNs this push accounts for, including the Rs objects the
            resolver materialises.  The order is deterministic and derived
            from the design, never from the order in which the controller
            answered: a parent always precedes its descendants.

            The **set** is mode-dependent, so never diff one mode's report
            against another's.  ``strict`` walks the whole design tree, so
            every declared object appears.  ``staged`` lists one entry per
            operation, and two curated kinds of class do not map one-to-one
            onto operations: a class marked *atomic* ships its whole subtree
            in a single request, so its children have no entry of their own,
            and a *carrier* — a path-only class the APIC materialises when a
            child posts beneath it — gets no operation and so no entry at all.
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
        external_refs: Per-reference verification statuses, populated only
            by ``push(mode="plan", verify_refs=True)`` — plan is the warn
            tier and never raises for a dangling reference; each entry is a
            :class:`~niwaki.design.RefCheck`.
    """

    creates: list[str]
    updates: dict[str, dict[str, tuple[Any, Any]]]
    unchanged: list[str]
    external_refs: list[RefCheck] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        """``True`` when a push would modify anything on the APIC."""
        return bool(self.creates or self.updates)


# ── Pure helpers ──────────────────────────────────────────────────────────────


def build_payload(root: DesignNode) -> dict[str, Any]:
    """Resolve the design and return the atomic ``polUni`` push payload."""
    return compile_poluni(root, resolve(root))


def _plan_roots(
    children: Iterable[DesignNode], prefix: str = "uni"
) -> Iterator[tuple[DesignNode, str]]:
    """Yield each diffable node with its full DN, descending through carriers.

    A curated carrier (a VMM provider, ``uni/vmmp-VMware``) is not itself diffed —
    the APIC rejects ``rsp-subtree`` on it — so the plan reads its children
    instead, at their full DNs under the carrier's RN.
    """
    from niwaki.design._cursor import _tables

    carrier = _tables().carrier
    for child in children:
        dn = f"{prefix}/{child.rn}"
        if child.aci_class in carrier:
            yield from _plan_roots(child.children, dn)
        else:
            yield child, dn


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
        # Matching is by wire class, not Python type: two catalogue-served
        # objects of different classes are both bare ManagedObject.
        if c is None or c.aci_class != d.aci_class or type(c) is not type(d):
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
            for wire, val in delta._raw_wire_attrs.items():  # pyright: ignore[reportPrivateUsage]
                # Wire-channel drift (raw nodes / raw_set escapes) — reported
                # under the wire name, both sides in wire spelling.
                updates[dn][wire] = (c.attrs.get(wire), val)

        current_children = {(child.aci_class, child.rn): child for child in c.children}
        for child in d.children:
            _walk(child, current_children.get((child.aci_class, child.rn)), f"{dn}/{child.rn}")

    _walk(desired, current, root_dn)
    return PlanResult(creates=creates, updates=updates, unchanged=unchanged)


def _index_desired(desired: ManagedObject, root_dn: str) -> dict[str, ManagedObject]:
    """``{dn: desired MO}`` for every node of one domain's desired tree."""
    index: dict[str, ManagedObject] = {}

    def _walk(mo: ManagedObject, dn: str) -> None:
        index[dn] = mo
        for child in mo.children:
            _walk(child, f"{dn}/{child.rn}")

    _walk(desired, root_dn)
    return index


def _absorb_found(
    dn: str,
    d: ManagedObject,
    c: ManagedObject,
    updates: dict[str, dict[str, tuple[Any, Any]]],
    unchanged: list[str],
) -> None:
    """Book a verified-present object as update or unchanged (pure)."""
    delta = mo_diff(d, c, recurse_children=False, respect_fields_set=True)
    if delta is None:
        unchanged.append(dn)
        return
    naming = set(type(d)._naming_props)  # pyright: ignore[reportPrivateUsage]
    fields = sorted(delta.model_fields_set - naming - {"children"})
    updates[dn] = {f: (getattr(c, f, None), getattr(d, f, None)) for f in fields}
    for wire, val in delta._raw_wire_attrs.items():  # pyright: ignore[reportPrivateUsage]
        updates[dn][wire] = (c.attrs.get(wire), val)


def _boundary(creates: set[str]) -> list[str]:
    """Creates whose parent is not itself a create — the only ones to verify."""
    from niwaki._dn import parent_dn

    return sorted(dn for dn in creates if (parent_dn(dn) or "") not in creates)


def _refine_part_sync(
    part: PlanResult, index: dict[str, ManagedObject], session: Any
) -> PlanResult:
    """Verify boundary creates with bare self-GETs — the scoped read is not gospel.

    Measured live: some objects never answer a class-scoped read
    (``quotaCont`` from any scope, ``maintLocalInstall`` from its domain
    root) and one class is not even a legal query argument (``fabSelfCA``,
    HTTP 400) — yet all of them answer a bare self-GET.  Reporting them as
    creates would make ``plan`` promise writes ``push`` then no-ops on.

    Cost model: one GET per **absent-subtree root** only — MIT containment
    guarantees a child cannot exist without its parent, so descendants of a
    confirmed-absent DN are never probed, and a greenfield design costs one
    GET per new domain, not one per object.
    """
    creates = set(part.creates)
    updates = dict(part.updates)
    unchanged = list(part.unchanged)
    settled: set[str] = set()  # probed and confirmed absent (or foreign)
    while True:
        frontier = [dn for dn in _boundary(creates) if dn not in settled]
        if not frontier:
            break
        for dn in frontier:
            d = index.get(dn)
            current = _fetch_bare_sync(session, dn) if d is not None else None
            if current is None or d is None or current.aci_class != d.aci_class:
                settled.add(dn)
                continue
            creates.discard(dn)
            _absorb_found(dn, d, current, updates, unchanged)
    return PlanResult(
        creates=[dn for dn in part.creates if dn in creates],
        updates=updates,
        unchanged=unchanged,
    )


def _fetch_bare_sync(session: Any, dn: str) -> ManagedObject | None:
    """Bare self-GET of one DN → typed MO, or ``None`` when absent."""
    from niwaki.exceptions import NotFoundError

    try:
        items = session.get(f"/api/mo/{dn}.json")
    except NotFoundError:
        return None
    return ManagedObject.from_apic(items[0]) if items else None


async def _refine_part_async(
    part: PlanResult, index: dict[str, ManagedObject], session: Any
) -> PlanResult:
    """Async twin of :func:`_refine_part_sync` — same probes, awaited."""
    from niwaki.exceptions import NotFoundError

    creates = set(part.creates)
    updates = dict(part.updates)
    unchanged = list(part.unchanged)
    settled: set[str] = set()
    while True:
        frontier = [dn for dn in _boundary(creates) if dn not in settled]
        if not frontier:
            break
        for dn in frontier:
            d = index.get(dn)
            current: ManagedObject | None = None
            if d is not None:
                try:
                    items = await session.get(f"/api/mo/{dn}.json")
                except NotFoundError:
                    items = []
                current = ManagedObject.from_apic(items[0]) if items else None
            if current is None or d is None or current.aci_class != d.aci_class:
                settled.add(dn)
                continue
            creates.discard(dn)
            _absorb_found(dn, d, current, updates, unchanged)
    return PlanResult(
        creates=[dn for dn in part.creates if dn in creates],
        updates=updates,
        unchanged=unchanged,
    )


def _drop_unqueryable(exc: Exception, remaining: list[str]) -> bool:
    """On APIC code 12 (*Unknown class X*), remove X from *remaining*.

    A few catalogue classes are not legal **query** arguments even though
    their objects exist and answer bare GETs (measured live: ``fabSelfCA``).
    A design can only reach one through a hand-written ``raw()``; dropping
    the class keeps the domain read alive, and the boundary-create probes
    still verify the object itself.

    Returns ``True`` when a class was dropped and the read should retry.
    """
    from niwaki.exceptions import APIError

    if not isinstance(exc, APIError) or exc.apic_code != "12":
        return False
    marker = "Unknown class "
    text = str(exc)
    if marker not in text:
        return False
    bad = text.rsplit(marker, 1)[1].split()[0]
    if bad not in remaining:
        return False
    remaining.remove(bad)
    return True


def _merge_plans(parts: list[PlanResult], external_refs: list[RefCheck]) -> PlanResult:
    """Aggregate per-domain plan results into one report."""
    return PlanResult(
        creates=[dn for part in parts for dn in part.creates],
        updates={dn: fields for part in parts for dn, fields in part.updates.items()},
        unchanged=[dn for part in parts for dn in part.unchanged],
        external_refs=external_refs,
    )


def _plan_classes(desired: ManagedObject) -> list[str]:
    """The classes one plan read is scoped to — R-3 at both ends of the scale.

    Unscoped, ``uni/fabric`` or ``uni/infra`` blows the APIC result limit
    ("result dataset is too big", HTTP 400); scoped into a *single* request, a
    large design's class list blows the request-line limit instead (measured:
    10 749 bytes of query string against a 4-8 KB ceiling).  The sharded flat
    reader (:func:`niwaki._read.read_subtree`) escapes both: any partition of
    this list is safe because the tree is rebuilt from DNs client-side.

    Every node of the desired tree contributes its class — including the
    intermediates — so every declared position's ancestors are part of the
    read and the rebuilt hierarchy stays connected.  Foreign instances of the
    same classes are ignored by the ``(class, rn)`` matcher.

    The class is read through the public ``aci_class`` accessor: a
    catalogue-served node (a reverse-imported ``raw()`` object) carries its
    wire class there, not in ``_aci_class`` — collecting the private field
    silently dropped those classes from the read and every raw node planned
    as a create (measured live: an appuser subtree, 34 phantom creates).
    """
    classes: set[str] = set()

    def _collect(mo: ManagedObject) -> None:
        if mo.aci_class:
            classes.add(mo.aci_class)
        for child in mo.children:
            _collect(child)

    _collect(desired)
    return sorted(classes)


def _staged_report(ops: list[_Op], outcome: _WaveOutcome) -> PushReport:
    """Turn an engine outcome into the public report, or raise on failure.

    Raises:
        StagedPushError: At least one operation failed; the exception carries
            the partial report plus the failed and skipped DNs.
    """
    report = PushReport(
        mode="staged",
        dns=[op.dn for op in outcome.succeeded],
        request_count=len(outcome.succeeded) + len(outcome.failed),
    )
    push_finished("staged", len(outcome.succeeded), len(outcome.failed), len(outcome.not_run))
    for op, error in outcome.failed:
        operation_failed(op.dn, error)
    if not outcome.ok:
        raise StagedPushError(
            report,
            failures=[(op.dn, exc) for op, exc in outcome.failed],
            not_run=[op.dn for op in outcome.not_run],
        )
    return report


# ── Sync execution ────────────────────────────────────────────────────────────


def push_sync(
    root: DesignNode,
    client: Niwaki,
    mode: PushMode,
    *,
    verify_refs: bool = False,
    max_concurrent: int | None = None,
) -> PushReport | PlanResult:
    """Execute a push through a sync :class:`~niwaki.Niwaki` client.

    See :meth:`niwaki.design.Cursor.push` for the full mode contract.
    """
    extras = resolve(root)
    session = client._sync_session  # pyright: ignore[reportPrivateUsage]

    checks: list[RefCheck] = []
    if verify_refs:
        refs = collect_external_refs(root, extras, set(_walk_dns(root, extras)))
        checks = verify_sync(session, refs)
        if mode != "plan" and (failures := failures_of(checks)):
            raise DanglingReferenceError(failures)

    if mode == "strict":
        dns = _walk_dns(root, extras)
        push_started("strict", len(dns))
        session.post_mo("uni", compile_poluni(root, extras))
        push_finished("strict", len(dns), 0, 0)
        return PushReport(mode="strict", dns=dns, request_count=1)

    if mode == "staged":
        ops = compile_ops(root, extras)

        def _execute(op: _Op) -> None:
            if op.method == "POST":
                session.post_mo(op.dn, op.payload or {})
            else:
                session.delete_mo(op.dn)

        push_started("staged", len(ops))
        return _staged_report(ops, _run_waves_sync(_execute, ops))

    # plan: one sharded read + diff per direct child of polUni (per declared
    # domain), scoped to the design's classes (R-3); boundary creates are
    # then verified by bare self-GETs (the scoped read is not gospel).
    parts: list[PlanResult] = []
    for child, child_dn in _plan_roots(root.children):
        desired = build_desired_tree(child, extras)
        classes = _plan_classes(desired)
        while True:
            try:
                current = read_subtree(session, child_dn, classes)
                break
            except Exception as exc:
                if not _drop_unqueryable(exc, classes):
                    raise
                if not classes:
                    current = None
                    break
        part = _plan_result(desired, current, child_dn)
        if part.creates:
            part = _refine_part_sync(part, _index_desired(desired, child_dn), session)
        parts.append(part)
    return _merge_plans(parts, checks)


# ── Async execution ───────────────────────────────────────────────────────────


async def push_async(
    root: DesignNode,
    client: AsyncNiwaki,
    mode: PushMode,
    *,
    verify_refs: bool = False,
    max_concurrent: int | None = None,
) -> PushReport | PlanResult:
    """Execute a push through an :class:`~niwaki.AsyncNiwaki` client.

    Mirror of :func:`push_sync` — validation, resolution, and compilation are
    the same pure code; only the three I/O calls are awaited.
    """
    extras = resolve(root)
    session = client._active_session  # pyright: ignore[reportPrivateUsage]

    # Throttle down, never up: the client's own limit is the ceiling, because a
    # push cannot make its session hand out more slots than the session owns.
    # Omitting the argument therefore reproduces earlier releases exactly.
    ceiling = client.max_concurrent
    bound = ceiling if max_concurrent is None else min(max_concurrent, ceiling)

    checks: list[RefCheck] = []
    if verify_refs:
        refs = collect_external_refs(root, extras, set(_walk_dns(root, extras)))
        checks = await verify_async(session, refs, concurrency=min(_VERIFY_CONCURRENCY, bound))
        if mode != "plan" and (failures := failures_of(checks)):
            raise DanglingReferenceError(failures)

    if mode == "strict":
        dns = _walk_dns(root, extras)
        push_started("strict", len(dns))
        await session.post_mo("uni", compile_poluni(root, extras))
        push_finished("strict", len(dns), 0, 0)
        return PushReport(mode="strict", dns=dns, request_count=1)

    if mode == "staged":
        ops = compile_ops(root, extras)
        push_started("staged", len(ops))
        return _staged_report(ops, await _run_waves(session, ops, max_concurrent=bound))

    # plan: one sharded read + diff per direct child of polUni (per declared
    # domain), scoped to the design's classes (R-3); boundary creates are
    # then verified by bare self-GETs (the scoped read is not gospel).
    parts: list[PlanResult] = []
    for child, child_dn in _plan_roots(root.children):
        desired = build_desired_tree(child, extras)
        classes = _plan_classes(desired)
        while True:
            try:
                current = await read_subtree_async(session, child_dn, classes)
                break
            except Exception as exc:
                if not _drop_unqueryable(exc, classes):
                    raise
                if not classes:
                    current = None
                    break
        part = _plan_result(desired, current, child_dn)
        if part.creates:
            part = await _refine_part_async(part, _index_desired(desired, child_dn), session)
        parts.append(part)
    return _merge_plans(parts, checks)
