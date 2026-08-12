"""Reconciliation — what the fabric carries that the design does not.

``push(mode="plan")`` answers one half of the drift question: what the
design declares that the fabric lacks or spells differently.  This module
answers the other half, **beside** plan (the 2.0 scoping's words): the
objects living under the design's declared domains that the design does
not declare.

Two kinds of undeclared object are not one story, so they report apart:

- **extra** — objects someone *created*: the operator-relevant signal.
- **implicit** — objects the fabric *materialises on its own* under
  declared parents (the default relations every BD grows, containers the
  controller mints).  Data-driven: a class the schema marks non-creatable
  cannot have been created by anyone, so it can never be an operator's
  leftover.  Without this split, every declared BD would drag its default
  ``fvRs*`` children into the report and drown the signal (measured).

Read-only by construction, and deliberately not a delete engine: the house
rule stands — a design never removes what it does not declare.
:func:`reconcile` informs; what to do about a foreign subtree is the
operator's decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from niwaki.design._cursor import Cursor
    from niwaki.facade import Niwaki
    from niwaki.snapshot import Snapshot


@dataclass(frozen=True)
class Reconciliation:
    """The fabric-side half of drift — objects the design does not declare.

    Attributes:
        extra: Every live, *creatable* object under the design's declared
            domains that the design does not declare, as sorted
            ``(dn, aci_class)`` pairs — things someone created.
        implicit: The undeclared objects whose class the schema marks
            non-creatable — fabric-materialised state (default relations,
            minted containers), never an operator's leftover.  Reported for
            completeness, excluded from :attr:`clean`.
        orphan_subtrees: The **minimal roots** of the ``extra`` regions —
            the extra objects whose parent is declared (or implicit, or a
            domain root).  The operator-granularity answer to "which whole
            subtrees are not mine?".
    """

    extra: list[tuple[str, str]]
    implicit: list[tuple[str, str]]
    orphan_subtrees: list[str]

    @property
    def clean(self) -> bool:
        """``True`` when the fabric carries nothing *created* beyond the design."""
        return not self.extra


def _never_creatable() -> frozenset[str]:
    """Classes the schema marks non-creatable — fabric-materialised state."""
    import sqlite3

    from niwaki.query import _catalog

    con = sqlite3.connect(f"file:{_catalog.DEFAULT_PATH}?mode=ro", uri=True)
    try:
        return frozenset(
            name
            for (name,) in con.execute(
                "SELECT class_name FROM mo WHERE is_creatable_deletable != 'always'"
            )
        )
    finally:
        con.close()


def _reconcile_against(declared: set[str], domains: set[str], snapshot: Snapshot) -> Reconciliation:
    """Pure half: one uni capture walked against the declared DNs.

    Only subtrees rooted at a *declared domain* (a declared direct child of
    ``polUni``, carriers included) are accounted: a domain the design does
    not claim at all is nobody's drift.
    """
    never = _never_creatable()
    extra: list[tuple[str, str]] = []
    implicit: list[tuple[str, str]] = []
    orphans: list[str] = []

    def _walk(node: dict[str, Any], dn: str, parent_owned: bool) -> None:
        is_declared = dn in declared
        if not is_declared:
            if node["class"] in never:
                implicit.append((dn, node["class"]))
            else:
                extra.append((dn, node["class"]))
                if parent_owned:
                    orphans.append(dn)
        for child in node["children"]:
            # "Owned" for orphan-root purposes: declared, or implicit under
            # an owned parent (a minted container does not break ownership).
            child_owned = is_declared or (
                not is_declared and node["class"] in never and parent_owned
            )
            _walk(child, f"{dn}/{child['rn']}", child_owned)

    tree = snapshot.tree
    if tree is None:
        return Reconciliation(extra=[], implicit=[], orphan_subtrees=[])
    for child in tree["children"]:
        dn = f"uni/{child['rn']}"
        if dn in domains:
            _walk(child, dn, True)
    return Reconciliation(
        extra=sorted(extra), implicit=sorted(implicit), orphan_subtrees=sorted(orphans)
    )


def reconcile(source: Cursor, client: Niwaki) -> Reconciliation:
    """Report what the fabric carries under this design's domains and the
    design does not declare — the other half of drift, beside ``plan``.

    One snapshot capture of the whole configuration (the same 15-or-so
    requests a backup costs, whatever the design's size) is walked against
    the design's own DNs, resolved references included.  Only the domains
    the design declares (its direct children of ``polUni``, carriers
    included) are accounted — a domain the design does not claim is
    nobody's drift.

    Reads only; nothing is ever written, and nothing is proposed for
    deletion — a design never removes what it does not declare.  Sync only,
    like :func:`niwaki.snapshot.take` which it reuses.

    Args:
        source: Any cursor of the design (the whole tree is taken, like
            ``push``).
        client: A connected :class:`~niwaki.Niwaki`.

    Returns:
        A :class:`Reconciliation`.  ``clean`` is ``True`` when the fabric
        holds nothing *created* beyond the design; fabric-materialised
        objects (default relations, minted containers — non-creatable
        classes) report separately under ``implicit``.

    Example::

        from niwaki import snapshot
        from niwaki.design import reconcile, to_design

        cfg = to_design(snapshot.take(aci, "uni"), redacted="skip")
        report = reconcile(cfg, aci)
        assert report.clean   # an imported design covers its own fabric

        # A partial hand design instead names what it does not own:
        for dn in report.orphan_subtrees:
            print("not mine:", dn)
    """
    from niwaki import snapshot
    from niwaki.design._push import _walk_dns
    from niwaki.design._resolver import resolve

    root = source.design_node.root()
    extras_map = resolve(root)
    declared = set(_walk_dns(root, extras_map)) | {"uni"}
    domains = {f"uni/{child.rn}" for child in root.children}
    return _reconcile_against(declared, domains, snapshot.take(client, "uni"))
