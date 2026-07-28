"""External-reference verification — the ``verify_refs`` push option.

A design may reference objects it does not declare: ``bind_dn`` aliases and
the literal-DN makers (``static_path``, ``path_attachment``, ``fc_path``, …)
carry raw DNs pushed on faith — and the APIC *accepts* a relation whose
target does not exist: the config lands, the relation stays unformed, and a
fault is the only trace. This module closes that last silent-failure gap:
enumerate every external DN a design references, read each one from the
APIC (reads only — the write path is untouched by construction), and report
a per-reference status. ``push(verify_refs=True)`` turns failures into a
:class:`~niwaki.exceptions.DanglingReferenceError` before anything is
written; ``plan`` mode attaches the statuses to
:attr:`~niwaki.design.PlanResult.external_refs` and never raises.

The expected-class authority is the read catalogue (the ``tCl`` accept-set
of the referencing Rs class, expanded through the full-schema subclass
walk) — never ``TARGET_SUBCLASSES``, whose generated-configurable scope
would reject valid read-only targets such as ``fabricPathEp``. An unknown
or empty accept-set degrades to an existence-only check, never a guess.

Pure core (enumeration, evaluation) + two thin I/O drivers (sync, async
with bounded concurrency), following the house split of
:mod:`niwaki.design._push`. Module level stays import-light — the catalogue
is consulted lazily, preserving the ``import niwaki.design`` cold-start
budget.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Literal

from niwaki import exceptions

if TYPE_CHECKING:
    from niwaki.design._node import DesignNode
    from niwaki.models.base import ManagedObject
    from niwaki.transport.session import ApicSession
    from niwaki.transport.session_async import AsyncApicSession

RefStatus = Literal["ok", "missing", "wrong_class", "unverifiable", "error"]

#: Wire prop values of ``tCl`` that name no concrete class.
_NON_CLASSES = frozenset({"defaultValue", "unspecified", ""})

#: Bounded fan-out for the async driver — verification is a read burst, not
#: a user-sized write wave.
_VERIFY_CONCURRENCY = 16


@dataclass(frozen=True, slots=True)
class ExternalRef:
    """One DN a design references without declaring it.

    Attributes:
        dn: The referenced DN, exactly as it would reach the wire.
        rs_class: The referencing class (an Rs class for ``bind_dn`` extras,
            the node's own class for literal-DN makers) — the source of the
            expected-class accept-set.
        declared_at: Human-readable design path of the declaring node, for
            error messages.
    """

    dn: str
    rs_class: str
    declared_at: str


@dataclass(frozen=True, slots=True)
class RefCheck:
    """The verification outcome for one external reference.

    Attributes:
        ref: The reference that was checked.
        status: ``ok`` (target exists and matches the accept-set),
            ``missing`` (no object at the DN), ``wrong_class`` (an object
            exists but its class is outside the referencing class's
            accept-set), ``unverifiable`` (the expected class is a carrier —
            a GET answers empty even for a valid path), or ``error`` (the
            read itself failed; see ``detail``).
        expected: The accept-set the target was checked against (empty when
            unknown — existence-only check).
        found: The wire class actually found at the DN, or ``None``.
        detail: Read-error text for ``status="error"``, else ``""``.
    """

    ref: ExternalRef
    status: RefStatus
    expected: tuple[str, ...]
    found: str | None
    detail: str = ""


# ── Pure core ─────────────────────────────────────────────────────────────────


def collect_external_refs(
    root: DesignNode,
    extras: dict[DesignNode, list[ManagedObject]],
    design_dns: set[str],
) -> list[ExternalRef]:
    """Enumerate every external DN reference of a resolved design.

    Two surfaces cover the complete compiled output (each node's own
    serialisation plus the resolver's Rs extras — nothing else reaches the
    wire):

    - resolver extras carrying a ``target_dn`` the caller supplied
      (``bind_dn`` aliases, including ``ref()``-overridden ones — the
      constructed MO is read, never the pending bind);
    - design nodes whose naming or attrs carry a ``target_dn`` (the
      literal-DN makers and ``.mo(RsClass, target_dn=...)`` escapes).

    DNs the design itself declares are skipped — this very push creates
    them. Deduplicated per ``(dn, rs_class)`` (the accept-set depends on the
    referencing class), deterministic order.

    Args:
        root: The design root (``polUni`` node).
        extras: The resolver output, as returned by
            :func:`niwaki.design._resolver.resolve`.
        design_dns: Every DN the design covers (see
            :func:`niwaki.design._push._walk_dns`) — the caller owns the
            walk so this core stays pure and import-free.

    Returns:
        The references to verify, sorted by ``(dn, rs_class)``.
    """
    seen: dict[tuple[str, str], ExternalRef] = {}

    def _add(dn: str, rs_class: str, declared_at: str) -> None:
        if not dn or dn in design_dns:
            return
        key = (dn, rs_class)
        if key not in seen:
            seen[key] = ExternalRef(dn=dn, rs_class=rs_class, declared_at=declared_at)

    def _walk(node: DesignNode) -> None:
        target = node.naming.get("target_dn") or node.attrs.get("target_dn")
        if isinstance(target, str):
            _add(target, node.cls.__name__, node.path())
        for rs in extras.get(node, []):
            if "target_dn" in rs.model_fields_set:
                dn = getattr(rs, "target_dn", None)
                if isinstance(dn, str):
                    _add(dn, type(rs).__name__, node.path())
        for child in node.children:
            _walk(child)

    _walk(root)
    return [seen[key] for key in sorted(seen)]


@cache
def _accept_set(rs_class: str) -> tuple[str, ...]:
    """Expected target classes for *rs_class*, from the catalogue's ``tCl``.

    The APIC's own accept-set (the ``tCl`` enum values), expanded through
    the full-schema subclass walk so read-only targets (``fabricPathEp``)
    are honored. Unknown class or no usable values → empty tuple, meaning
    existence-only verification.
    """
    from niwaki import catalog

    try:
        meta = catalog.prop_meta(rs_class, "tCl")
    except (KeyError, LookupError):
        return ()
    accepted: set[str] = set()
    for value in meta.enum_values or ():
        if value in _NON_CLASSES:
            continue
        accepted.add(value)
        with contextlib.suppress(KeyError, LookupError):
            accepted.update(catalog.concrete_subclasses(value))
    return tuple(sorted(accepted))


def _carrier_classes() -> frozenset[str]:
    from niwaki.design._cursor import _tables

    return frozenset(_tables().carrier)


def evaluate(ref: ExternalRef, imdata: list[dict[str, object]] | None) -> RefCheck:
    """Turn one read result into a :class:`RefCheck` (pure).

    Args:
        ref: The reference that was read.
        imdata: The raw envelope list the APIC returned for the DN, or
            ``None`` when the read was skipped (carrier target).
    """
    expected = _accept_set(ref.rs_class)
    if imdata is None:
        return RefCheck(ref=ref, status="unverifiable", expected=expected, found=None)
    if not imdata:
        return RefCheck(ref=ref, status="missing", expected=expected, found=None)
    first = imdata[0]
    found = next(iter(first), None)
    if expected and found not in expected:
        return RefCheck(ref=ref, status="wrong_class", expected=expected, found=found)
    return RefCheck(ref=ref, status="ok", expected=expected, found=found)


def _is_carrier_only(ref: ExternalRef) -> bool:
    expected = _accept_set(ref.rs_class)
    return bool(expected) and set(expected) <= _carrier_classes()


def failures_of(checks: list[RefCheck]) -> list[RefCheck]:
    """The checks that must block a write push (never ``unverifiable``)."""
    return [c for c in checks if c.status in ("missing", "wrong_class", "error")]


# ── I/O drivers ───────────────────────────────────────────────────────────────


def verify_sync(session: ApicSession, refs: list[ExternalRef]) -> list[RefCheck]:
    """Read every unique DN once and evaluate all references (sync).

    Reads only — one ``GET /api/mo/{dn}.json`` per unique DN; a per-read
    :class:`~niwaki.exceptions.APIError` becomes an ``error`` status so one
    malformed DN can never abort the collection.
    """
    fetched: dict[str, list[dict[str, object]] | Exception] = {}
    for dn in sorted({r.dn for r in refs if not _is_carrier_only(r)}):
        try:
            fetched[dn] = session.get(f"/api/mo/{dn}.json")
        except exceptions.APIError as exc:  # aggregated, never first-fail
            fetched[dn] = exc
    return _evaluate_all(refs, fetched)


async def verify_async(
    session: AsyncApicSession,
    refs: list[ExternalRef],
    *,
    concurrency: int = _VERIFY_CONCURRENCY,
) -> list[RefCheck]:
    """Async mirror of :func:`verify_sync`, with bounded concurrency."""
    semaphore = asyncio.Semaphore(concurrency)
    fetched: dict[str, list[dict[str, object]] | Exception] = {}

    async def _fetch(dn: str) -> None:
        async with semaphore:
            try:
                fetched[dn] = await session.get(f"/api/mo/{dn}.json")
            except exceptions.APIError as exc:
                fetched[dn] = exc

    unique = sorted({r.dn for r in refs if not _is_carrier_only(r)})
    async with asyncio.TaskGroup() as group:
        for dn in unique:
            group.create_task(_fetch(dn))
    return _evaluate_all(refs, fetched)


def _evaluate_all(
    refs: list[ExternalRef],
    fetched: dict[str, list[dict[str, object]] | Exception],
) -> list[RefCheck]:
    checks: list[RefCheck] = []
    for ref in refs:
        if _is_carrier_only(ref):
            checks.append(evaluate(ref, None))
            continue
        got = fetched[ref.dn]
        if isinstance(got, Exception):
            checks.append(
                RefCheck(
                    ref=ref,
                    status="error",
                    expected=_accept_set(ref.rs_class),
                    found=None,
                    detail=str(got),
                )
            )
        else:
            checks.append(evaluate(ref, got))
    return checks
