"""Bulk class-scoped reads — sharded flat queries, tree rebuilt by DN.

Reading many classes under one scope DN is the substrate of the plan diff (and,
ahead, of the fabric snapshot).  The obvious single request —
``rsp-subtree=full`` with every class in ``rsp-subtree-class`` — dies at both
ends of the scale (R-3): unscoped it blows the APIC result limit ("result
dataset is too big"), and scoped to a large design the class list alone exceeds
the APIC's request-line limit (measured live: a 19 736-byte class list answers
HTTP 414 Request-URI Too Large).

This module bounds both ends:

1. **Shard** the class list into chunks that each fit a conservative
   query-string budget — the request side.
2. **Read flat and paginated** — ``query-target=subtree`` +
   ``target-subtree-class`` per shard, through the transport's auto-pagination
   — the response side.  Any partition of the classes is safe, because each
   object carries its ``dn`` and the tree is rebuilt client-side; a nested
   ``rsp-subtree`` read would instead demand ancestor-closed shards to keep
   the returned hierarchy connected.
3. **Rebuild** the containment tree from the DNs with :mod:`niwaki._dn`.

One shard — the common case — degenerates to a single request, so every caller
exercises the same path regardless of design size.

Two honest caveats, both inherent to reading a live system in pieces:

- **The merged result is not one atomic snapshot.**  Two shards (or two pages)
  answer at different instants; a writer racing the read can leave the rebuilt
  tree in a combination no single moment held.  The old single-request read
  had the same property across its pages-to-be and across domains — the
  window is merely named here.
- **Typed deserialisation needs the model REGISTRY populated** (it fills as
  generated classes are imported).  The plan path guarantees this — the
  desired tree instantiates every declared class before the read — but a
  standalone caller reading arbitrary classes gets base ``ManagedObject``
  nodes for anything not imported.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

from niwaki._dn import parent_dn
from niwaki.exceptions import DeserializationError
from niwaki.models.base import ManagedObject

# Budget in bytes for one ``target-subtree-class`` value AS ENCODED ON THE
# WIRE.  The APIC front end caps the request line at 4-8 KB depending on
# deployment; 3 500 bytes of encoded class list leaves headroom for scheme,
# host, path, and the other parameters even on the 4 KB floor.  The separator
# is costed at 3 bytes because httpx percent-encodes ``,`` to ``%2C``.
_CLASS_LIST_BUDGET = 3500
_SEPARATOR_COST = len("%2C")

# Objects per page for the paginated shard reads — the transport default.
_PAGE_SIZE = 500


class _PagedReader(Protocol):
    """The slice of a session the reader drives — the auto-paginating GET.

    This is the same seam the query builder drives on both concrete sessions;
    plain ``get`` would return a single page and reintroduce the response-size
    cliff on high-cardinality classes.
    """

    def _get_all_pages(
        self, path: str, params: dict[str, Any], *, page_size: int = ...
    ) -> list[dict[str, Any]]: ...


class _AsyncPagedReader(Protocol):
    """Async twin of :class:`_PagedReader`."""

    async def _get_all_pages(
        self, path: str, params: dict[str, Any], *, page_size: int = ...
    ) -> list[dict[str, Any]]: ...


def shard_classes(classes: Iterable[str], *, budget: int | None = None) -> list[str]:
    """Partition class names into comma-joined lists that each fit *budget*.

    Deterministic: input order is irrelevant (names are sorted and
    de-duplicated), so the same class set always produces the same shards —
    and therefore the same requests, in the same order.

    Args:
        classes: ACI class names, in any order, duplicates tolerated.
        budget: Maximum length in bytes of one joined list **as percent-encoded
            on the wire** (each ``,`` costs 3 bytes, ``%2C``); ``None`` (the
            default) resolves to ``_CLASS_LIST_BUDGET`` at call time.  A single
            name longer than the budget still ships, alone in its own shard —
            a name cannot be split.

    Returns:
        The comma-joined shards, in sorted-name order.  Empty input yields no
        shards.
    """
    if budget is None:
        budget = _CLASS_LIST_BUDGET
    shards: list[str] = []
    current: list[str] = []
    size = 0
    for name in sorted(set(classes)):
        extra = len(name) + (_SEPARATOR_COST if current else 0)
        if current and size + extra > budget:
            shards.append(",".join(current))
            current, size = [], 0
            extra = len(name)
        current.append(name)
        size += extra
    if current:
        shards.append(",".join(current))
    return shards


def build_tree(items: Iterable[dict[str, Any]], root_dn: str) -> ManagedObject | None:
    """Rebuild the containment tree from flat class-query response items.

    Each item is one APIC envelope (``{"fvBD": {"attributes": {...}}}``) whose
    attributes carry the object's ``dn`` — the flat form always does.  Nodes
    are attached to their parent by DN (bracket-aware, so a path DN inside an
    RN never splits a segment).

    An item whose parent DN is absent from the result set is **dropped**: it is
    an instance of a requested class living at a position whose ancestors were
    not requested — a foreign position the caller's scope does not cover.  For
    a plan read this cannot lose a declared object, because every ancestor
    class of a declared position is part of the request.

    Args:
        items: Flat envelope dicts, from any number of shard responses, in any
            order.  A duplicate DN keeps the last occurrence (shards never
            overlap, but a paginated read racing a live fabric can echo a
            boundary object on two pages).
        root_dn: The scope DN the read was rooted at.

    Returns:
        The tree rooted at *root_dn*, or ``None`` when the root itself was not
        in the results (the scope object does not exist on the APIC — nothing
        under it can either).

    Raises:
        DeserializationError: An item is not a one-key APIC envelope, or
            carries no ``dn`` — the flat form guarantees both, so a violation
            means the response is not what this function is for.  Typed, so a
            caller's ``except NiwakiError`` sees it like every other failure
            of the same read.
    """
    nodes: dict[str, ManagedObject] = {}
    for item in items:
        if len(item) != 1:
            raise DeserializationError(f"flat read item is not a one-key envelope: {item!r}")
        (body,) = item.values()
        dn = body.get("attributes", {}).get("dn")
        if not dn:
            raise DeserializationError(f"flat read item has no dn: {item!r}")
        nodes[dn] = ManagedObject.from_apic(item)

    root = nodes.get(root_dn)
    if root is None:
        return None
    for dn, node in nodes.items():
        if dn == root_dn:
            continue
        parent = nodes.get(parent_dn(dn) or "")
        if parent is not None:
            parent.children.append(node)
    return root


def _shard_params(shard: str) -> dict[str, Any]:
    return {"query-target": "subtree", "target-subtree-class": shard}


def read_subtree(
    session: _PagedReader,
    dn: str,
    classes: Iterable[str],
    *,
    budget: int | None = None,
) -> ManagedObject | None:
    """Read every instance of *classes* under *dn* and rebuild their tree.

    Issues one flat, auto-paginated GET per shard (usually exactly one shard)
    and hands the merged results to :func:`build_tree`.

    Args:
        session: A concrete APIC session — anything exposing the transport's
            auto-paginating ``_get_all_pages``.
        dn: Scope DN — the subtree root.  Its own class must be part of
            *classes* for the root to appear in the result.
        classes: The class names to read.  Must include every ancestor class
            of any position the caller intends to find, or those positions
            will be dropped as foreign.
        budget: See :func:`shard_classes`.

    Returns:
        The rebuilt tree, or ``None`` when nothing exists at *dn*.
    """
    items: list[dict[str, Any]] = []
    for shard in shard_classes(classes, budget=budget):
        items.extend(
            session._get_all_pages(f"/api/mo/{dn}.json", _shard_params(shard), page_size=_PAGE_SIZE)
        )
    return build_tree(items, dn)


async def read_subtree_async(
    session: _AsyncPagedReader,
    dn: str,
    classes: Iterable[str],
    *,
    budget: int | None = None,
) -> ManagedObject | None:
    """Async twin of :func:`read_subtree` — same requests, awaited in order."""
    items: list[dict[str, Any]] = []
    for shard in shard_classes(classes, budget=budget):
        items.extend(
            await session._get_all_pages(
                f"/api/mo/{dn}.json", _shard_params(shard), page_size=_PAGE_SIZE
            )
        )
    return build_tree(items, dn)
