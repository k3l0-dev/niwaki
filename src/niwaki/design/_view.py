"""DesignView — the frozen, walkable projection of a design.

A design is built through cursors and, until now, could only be *serialised*
(``to_payload``) — it could not be walked: no public way to enumerate what a
design declares, look an object up by DN, or read the references placed on a
node.  :class:`DesignView` closes that gap (a measured public-API hole of the
2.0 scoping): one call captures the whole tree as an immutable value object.

The view is a **snapshot of the design at call time** — editing the design
through its cursors afterwards does not change an existing view; take a new
one.  It carries everything needed to *regenerate* the declarations — maker
label and position, naming, attributes as declared, wire-channel escapes,
and every reference with its configuration — which is exactly the contract
the 2.0 code emitter consumes (``to_code`` takes a view, never a snapshot).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Iterator

    from niwaki.design._node import DesignNode


@dataclass(frozen=True)
class DesignViewBind:
    """One reference declared on a design node, as the caller declared it.

    Attributes:
        kind: ``"bind"`` (closed-world alias), ``"bind_dn"`` (raw-DN escape)
            or ``"verb"`` (curated contract verb such as ``provide``).
        alias: The vocabulary word used at the call site (``"vrf"``,
            ``"provide"``, …).
        target_class: ACI class the reference points at — possibly abstract
            for curated aliases (``"infraDomP"``).
        target: The target's primary name — or its raw DN for ``"bind_dn"``.
        attrs: Configuration carried by the relation itself
            (:func:`~niwaki.design.ref` attributes), readable names.  Empty
            for a pure edge.
    """

    kind: Literal["bind", "bind_dn", "verb"]
    alias: str
    target_class: str
    target: str
    attrs: dict[str, Any]


@dataclass(frozen=True)
class DesignViewNode:
    """One declared object of a design, projected read-only.

    Attributes:
        dn: The DN this object will occupy once pushed.
        aci_class: Wire class name (``"fvBD"``).
        label: The maker that created the node (``"bd"``), or the class name
            for escape-hatch nodes (``.mo()`` / ``.raw()``).
        position: Dotted maker path from the ``polUni`` root (``"tenant.bd"``),
            ``""`` for the root itself, ``None`` for nodes outside the curated
            vocabulary.
        naming: Naming property values.  Readable names for typed nodes; wire
            names for catalogue-served ``raw()`` nodes (their whole surface is
            wire-spelled).
        attrs: Non-naming attributes exactly as declared (readable names,
            pre-coercion values).
        raw_attrs: Wire-channel escapes (``raw_set()`` and the reverse
            importer's escapes) — wire names, wire string values.
        binds: The references declared on this node, in declaration order.
        children: Child nodes, in declaration order.
    """

    dn: str
    aci_class: str
    label: str
    position: str | None
    naming: dict[str, Any]
    attrs: dict[str, Any]
    raw_attrs: dict[str, str]
    binds: tuple[DesignViewBind, ...]
    children: tuple[DesignViewNode, ...]


class DesignView:
    """A whole design, frozen and walkable.

    Obtain one from :meth:`niwaki.design.Cursor.view`.  Iteration yields
    every node parents-first in declaration order (the ``polUni`` root
    included); lookups are by DN.

    Example::

        from niwaki.design import tenant

        cfg = tenant("prod")
        cfg.bd("web").bind(vrf="main")
        cfg.vrf("main")

        view = cfg.view()
        [n.aci_class for n in view]
        # → ['polUni', 'fvTenant', 'fvBD', 'fvCtx']
        view["uni/tn-prod/BD-web"].binds[0].alias   # → 'vrf'
        [n.dn for n in view.by_class("fvCtx")]      # → ['uni/tn-prod/ctx-main']
    """

    __slots__ = ("_by_class", "_by_dn", "_root")

    def __init__(self, root: DesignViewNode) -> None:
        self._root = root
        self._by_dn: dict[str, DesignViewNode] = {}
        self._by_class: dict[str, list[DesignViewNode]] = {}

        def _index(node: DesignViewNode) -> None:
            self._by_dn[node.dn] = node
            self._by_class.setdefault(node.aci_class, []).append(node)
            for child in node.children:
                _index(child)

        _index(root)

    @property
    def root(self) -> DesignViewNode:
        """The ``polUni`` root node of the design."""
        return self._root

    def __iter__(self) -> Iterator[DesignViewNode]:
        def _walk(node: DesignViewNode) -> Iterator[DesignViewNode]:
            yield node
            for child in node.children:
                yield from _walk(child)

        return _walk(self._root)

    def __len__(self) -> int:
        return len(self._by_dn)

    def __contains__(self, dn: object) -> bool:
        return dn in self._by_dn

    def __getitem__(self, dn: str) -> DesignViewNode:
        """The node at *dn*.

        Raises:
            KeyError: No object at that DN is declared in this design.
        """
        return self._by_dn[dn]

    def get(self, dn: str) -> DesignViewNode | None:
        """The node at *dn*, or ``None`` when the design does not declare it."""
        return self._by_dn.get(dn)

    def by_class(self, aci_class: str) -> tuple[DesignViewNode, ...]:
        """Every declared node of *aci_class*, in declaration order."""
        return tuple(self._by_class.get(aci_class, ()))

    def __repr__(self) -> str:
        return f"<DesignView {len(self)} node(s), root {self._root.aci_class!r}>"


def build_view(root: DesignNode) -> DesignView:
    """Project a design tree into a :class:`DesignView` (internal builder).

    Values are deep-copied: the view is a value object and mutating one of
    its dicts (or a mutable value inside, a Flags ``set``) must never reach
    back into the design.
    """
    import copy

    def _project(node: DesignNode, dn: str) -> DesignViewNode:
        binds = tuple(
            DesignViewBind(
                kind=bind.kind,
                alias=bind.alias,
                target_class=bind.target_aci_class,
                target=bind.target_name,
                attrs=copy.deepcopy(bind.attrs),
            )
            for bind in node.binds
        )
        children = tuple(_project(child, f"{dn}/{child.rn}") for child in node.children)
        return DesignViewNode(
            dn=dn,
            aci_class=node.aci_class,
            label=node.label,
            position=node.position,
            naming=copy.deepcopy(getattr(node, "wire_naming", None) or node.naming),
            attrs=copy.deepcopy(node.attrs),
            raw_attrs=dict(node.raw_attrs),
            binds=binds,
            children=children,
        )

    return DesignView(_project(root, root.rn))
