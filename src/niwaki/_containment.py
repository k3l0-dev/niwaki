"""Resolve a DN to its ACI class by walking the containment tables — no I/O.

A DN names an object, not its class: ``uni/tn-prod/BD-web`` is an ``fvBD``
only because ``BD-web`` fits ``fvBD``'s RN format *among the children of*
``fvTenant``, which ``tn-prod`` is among the children of ``polUni``.  This
module performs that walk, RN segment by RN segment from ``uni``, for every
caller that must turn a DN back into a class without asking the fabric:

- the reverse import's scope walk (:func:`niwaki.design.to_design` on a
  scoped snapshot rebuilds the attribute-less ancestors of the capture);
- the façade's :meth:`~niwaki.Niwaki.node` without an explicit class.

It is shared, and private, so neither caller imports the other: the façade
observes and must never load the design DSL.

The authorities, by precedence:

1. the **tables** — ``CHILD_MAP`` (the navigation tables, the top level keyed
   ``"_root"``) together with the generated model's ``_contains`` (the
   schema's containment edges);
2. the catalogue's **DN grammar** — a class whose ``dn_formats`` place the
   parent's RN format immediately above its own — consulted *only* when the
   tables match nothing.  Measured live, some edges exist only there
   (``uiSettingsCont`` under ``polUni``, ``aaaAppUser`` under ``aaaUserEp``,
   ``commTelnet`` under ``commPol``).

The precedence is a decision, not a tie-break: a segment the tables match is
never re-examined against the grammar, so a class the grammar alone would
also accept loses silently.  One such case is known — ``vzSubj`` catalogues
the format ``uni/tn-{name}/subj-{name}``, the very format of the route-control
subject ``rtctrlSubjP`` that the tenant's tables hold, so ``uni/tn-x/subj-s``
resolves to ``rtctrlSubjP`` (the class a real fabric has there).

Within one authority the walk never guesses.  The tables are unambiguous by
measurement (zero RN-format skeleton collisions inside any of the 3 049
containment tables — the 117 RN prefixes shared across the schema are
ambiguous *globally*, never among one parent's children); the grammar is
not (``uni/infra/nodecfgcont/node-101`` fits both ``infraNodeCfg`` and
``fabricNodeCfg``), and a segment it matches several times raises, naming
every candidate.

The outcome of a segment is a pure function of ``(parent class, segment)``:
it is memoised in a bounded LRU cache, failures and ambiguities included, so
repeated walks cost lookups.
"""

from __future__ import annotations

from functools import cache, lru_cache
from importlib import import_module
from typing import TYPE_CHECKING

from niwaki._dn import _has_parse, split_dn

if TYPE_CHECKING:
    from niwaki.models.base import ManagedObject

ROOT_CLASS = "polUni"
"""The class at DN ``uni`` — the policy universe every walk starts from."""

ROOT_RN = "uni"
"""The RN (and DN) of :data:`ROOT_CLASS`."""

ROOT_ROW = "_root"
"""The key under which ``CHILD_MAP`` stores :data:`ROOT_CLASS`'s vocabulary."""


class UnresolvedSegmentError(ValueError):
    """An RN segment matches no child class of its parent, or several.

    Attributes:
        parent_cls: The class the segment was resolved under.
        segment: The RN segment.
        matches: The matching child classes, sorted — empty when none
            matched, two or more when the segment is ambiguous.
    """

    def __init__(self, parent_cls: str, segment: str, matches: list[str]) -> None:
        self.parent_cls = parent_cls
        self.segment = segment
        self.matches = sorted(matches)
        if self.matches:
            listed = ", ".join(self.matches)
            detail = f"RN segment {segment!r} is ambiguous under {parent_cls} ({listed})."
        else:
            detail = f"no child class of {parent_cls} matches the RN segment {segment!r}."
        super().__init__(detail)


class UnresolvedDnError(ValueError):
    """A DN cannot be walked to a class from ``uni``.

    Attributes:
        dn: The DN.
        reason: Why the walk stopped (outside ``uni``, or the message of
            the :class:`UnresolvedSegmentError` of the segment that failed).
        segment: The RN segment the walk stopped at (``None`` when the DN
            is outside ``uni``).
        candidates: The classes that segment matches, sorted — two or more
            when it is ambiguous, empty when it matches nothing.
    """

    def __init__(
        self,
        dn: str,
        reason: str,
        *,
        segment: str | None = None,
        candidates: tuple[str, ...] = (),
    ) -> None:
        self.dn = dn
        self.reason = reason
        self.segment = segment
        self.candidates = candidates
        super().__init__(f"cannot resolve the class of {dn!r}: {reason}")

    @property
    def ambiguous(self) -> bool:
        """Whether the walk stopped on a segment several classes match."""
        return len(self.candidates) > 1


@cache
def load_model(cls_name: str) -> type[ManagedObject] | None:
    """The generated model of *cls_name*, imported lazily — ``None`` without one.

    Args:
        cls_name: An ACI class name, e.g. ``"fvBD"`` or ``"faultInst"``.

    Returns:
        The model class, or ``None`` for a class the SDK generates no model
        for (a catalogue-only class such as ``faultInst``).
    """
    from niwaki.domain._child_map import CLASS_PKG
    from niwaki.models.base import ManagedObject

    pkg = CLASS_PKG.get(cls_name)
    if pkg is None:
        return None
    model: object = getattr(import_module(f"niwaki.models._generated.{pkg}.{cls_name}"), cls_name)
    if not (isinstance(model, type) and issubclass(model, ManagedObject)):
        return None
    return model


def _contains(cls_name: str) -> frozenset[str]:
    """The generated model's ``_contains`` (empty for a class without a model)."""
    model = load_model(cls_name)
    return frozenset() if model is None else model._contains  # pyright: ignore[reportPrivateUsage]


@cache
def _rn_format_rows() -> tuple[tuple[str, str], ...]:
    """Every catalogue ``(class_name, rn_format)`` pair, scanned once."""
    from niwaki.query._catalog import catalog

    return tuple(catalog()._rn_format_rows())  # pyright: ignore[reportPrivateUsage]


def grammar_children(parent_cls: str, segment: str) -> list[str]:
    """Classes the DN grammar proves under *parent_cls* that match *segment*.

    The slow path of :func:`child_class`, walked only when neither CHILD_MAP
    nor the generated ``_contains`` knows the edge.  Shape-matches the
    segment against every catalogued RN format, then keeps only the classes
    whose ``dn_formats`` place the parent's RN format immediately above.

    Args:
        parent_cls: The parent's class name.
        segment: The child's RN segment.

    Returns:
        The matching class names, in catalogue order (possibly empty).
    """
    from niwaki.query._catalog import catalog

    parent_rn = ROOT_RN if parent_cls == ROOT_CLASS else catalog().rn_format(parent_cls)
    if not parent_rn:
        return []
    matches: list[str] = []
    for cls_name, rn_fmt in _rn_format_rows():
        if not _has_parse(segment, rn_fmt):
            continue  # one parse — ambiguous or not — would prove the shape
        for fmt in catalog().dn_formats(cls_name):
            segments = split_dn(fmt)
            if len(segments) >= 2 and segments[-2] == parent_rn:
                matches.append(cls_name)
                break
    return matches


def child_class(parent_cls: str, segment: str) -> str:
    """Resolve one RN segment to its class among *parent_cls*'s children.

    The tables (``CHILD_MAP`` and ``_contains``) win; the DN grammar is
    consulted only when they match nothing (see the module docstring).

    Args:
        parent_cls: The parent's class name (``"polUni"`` at the top level).
        segment: One RN segment, e.g. ``"BD-web"``.

    Returns:
        The child's class name — generated model or catalogue-only class.

    Raises:
        UnresolvedSegmentError: No child class of *parent_cls* matches
            *segment*, or more than one does (in practice only the DN
            grammar is ever ambiguous).
    """
    matches = _segment_matches(parent_cls, segment)
    if len(matches) == 1:
        return matches[0]
    raise UnresolvedSegmentError(parent_cls, segment, list(matches))


@lru_cache(maxsize=4096)
def _segment_matches(parent_cls: str, segment: str) -> tuple[str, ...]:
    """Every class *segment* may be under *parent_cls* — memoised, bounded.

    The pure outcome :func:`child_class` decides on: one class, none, or
    several.  Caching the tuple (rather than the class) memoises the failures
    and the ambiguities too — an unresolvable segment otherwise rescans the
    whole catalogue on every walk.
    """
    from niwaki.domain._child_map import CHILD_MAP
    from niwaki.query._catalog import catalog

    table_key = ROOT_ROW if parent_cls == ROOT_CLASS else parent_cls
    candidates = set(CHILD_MAP.get(table_key, {}).values()) | _contains(parent_cls)

    matches: list[str] = []
    for cls_name in candidates:
        try:
            rn_fmt = catalog().rn_format(cls_name)
        except KeyError:
            continue
        # One parse — ambiguous or not — proves the segment fits the class.
        if _has_parse(segment, rn_fmt):
            matches.append(cls_name)
    if not matches:
        matches = grammar_children(parent_cls, segment)
    return tuple(matches)


def class_of_dn(dn: str) -> str:
    """The class of the object at *dn*, walked from ``uni`` — no request is made.

    Args:
        dn: A Distinguished Name, e.g. ``"uni/tn-prod/BD-web"``.

    Returns:
        The class name (``"fvBD"``), which may be a catalogue-only class with
        no generated model (``"faultInst"``).

    Raises:
        UnresolvedDnError: *dn* is outside ``uni``, or one of its segments
            matches no child class (or several) of its parent.

    Example:
        >>> class_of_dn("uni/tn-prod/BD-web")  # doctest: +SKIP
        'fvBD'
    """
    segments = split_dn(dn)
    if not segments or segments[0] != ROOT_RN:
        raise UnresolvedDnError(dn, f"the DN is outside {ROOT_RN!r}")
    cls_name = ROOT_CLASS
    for segment in segments[1:]:
        try:
            cls_name = child_class(cls_name, segment)
        except UnresolvedSegmentError as exc:
            raise UnresolvedDnError(
                dn, str(exc), segment=segment, candidates=tuple(exc.matches)
            ) from None
    return cls_name
