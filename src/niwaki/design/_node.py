"""Design tree internals — :class:`DesignNode` and :class:`PendingBind`.

A design is a detached, in-memory tree of :class:`DesignNode` objects.  Each
node knows its ACI class, naming props, accumulated scalar attributes, its
parent, its structural children, and the references (`bind`/`provide`/
`consume`) declared on it.  No I/O ever happens here.

Design decision: the node owns the topology — the underlying
:class:`~niwaki.models.base.ManagedObject` instances are constructed fresh on
demand with an **empty** ``children`` list, so a design can be compiled and
pushed any number of times without mutating user-visible objects or
duplicating children.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal

from niwaki.models.base import ManagedObject

BindKind = Literal["bind", "bind_dn", "provide", "consume"]
BindFlavor = Literal["name", "dn"]


@dataclass(frozen=True)
class PendingBind:
    """A lazily-resolved reference declared on a design node.

    Attributes:
        kind: ``"bind"`` for closed-world Rs edges, ``"bind_dn"`` for raw-DN
            escapes (no lookup), ``"provide"``/``"consume"`` for EPG contract
            verbs.
        alias: The vocabulary word used at the call site (``"vrf"``,
            ``"filter"``, ``"provide"`` …) — kept for error messages.
        target_aci_class: ACI class name of the referenced object — possibly
            abstract for curated aliases (e.g. ``"infraDomP"``).
        target_name: Primary naming value of the referenced object, or the
            raw DN for ``"bind_dn"``.
        rs_aci_class: Relationship ACI class name when known upfront (verbs,
            ``bind_dn``); empty for ``"bind"`` — resolved at push time via
            ``REFERENCE_MAP``.
        flavor: How the Rs targets — ``"name"`` (``tn*`` prop) or ``"dn"``
            (``tDn``).  Fixed upfront for verbs and ``bind_dn``; ``None`` for
            ``"bind"`` — resolved at push time with the Rs class.
    """

    kind: BindKind
    alias: str
    target_aci_class: str
    target_name: str
    rs_aci_class: str = ""
    flavor: BindFlavor | None = None


class DesignNode:
    """One declared object in a design tree.

    Args:
        cls: Generated :class:`~niwaki.models.base.ManagedObject` subclass.
        label: The maker name used to create this node (``"bd"``, ``"epg"``,
            or the class name for ``.mo()`` escapes) — used in error messages.
        naming: Naming prop values (e.g. ``{"name": "web"}``).
        attrs: Non-naming scalar attributes accumulated via makers / ``set()``.
        parent: Parent node, or ``None`` for the design root.
        position: Dotted maker path from the ``polUni`` root (``""`` for the
            root itself, ``"infra.leaf_profile.leaf_selector"`` for curated
            positions).  ``None`` for nodes outside the curated vocabulary
            (``.mo()`` escapes) — those dispatch to the base cursor.
    """

    __slots__ = ("attrs", "binds", "children", "cls", "label", "naming", "parent", "position")

    def __init__(
        self,
        cls: type[ManagedObject],
        label: str,
        naming: dict[str, Any],
        attrs: dict[str, Any],
        parent: DesignNode | None,
        *,
        position: str | None = None,
    ) -> None:
        self.cls = cls
        self.label = label
        self.naming = naming
        self.attrs = attrs
        self.parent = parent
        self.position = position
        self.children: list[DesignNode] = []
        self.binds: list[PendingBind] = []

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def aci_class(self) -> str:
        """ACI class name of this node (e.g. ``"fvBD"``)."""
        return self.cls._aci_class  # pyright: ignore[reportPrivateUsage]

    @property
    def primary_name(self) -> str:
        """Value of the first naming prop, or ``""`` for singletons."""
        props: list[str] = self.cls._naming_props  # pyright: ignore[reportPrivateUsage]
        if not props:
            return ""
        return str(self.naming.get(props[0], ""))

    def mo(self) -> ManagedObject:
        """Construct a fresh, validated MO from this node's naming + attrs.

        The instance is built through the regular Pydantic constructor so all
        field constraints apply.  Its ``children`` list is empty — the design
        topology lives on the node, not on the model instance.
        """
        return self.cls(**self.naming, **self.attrs)

    @property
    def rn(self) -> str:
        """Relative Name computed from the class RN format and naming props."""
        return self.mo().rn

    def dn(self) -> str:
        """Distinguished Name this node will occupy once pushed.

        The design root is a ``polUni`` node whose RN is ``"uni"`` — the
        joined ancestor RNs therefore form the full DN with no hardcoded
        prefix (e.g. ``"uni/tn-prod/BD-web"``).
        """
        segments = [node.rn for node in self.ancestors_and_self()]
        return "/".join(reversed(segments))

    # ── Navigation ────────────────────────────────────────────────────────────

    def path(self) -> str:
        """Human-readable ancestor path for error messages.

        Returns:
            A string such as ``tenant 'prod' → bd 'web'``.
        """
        parts: list[str] = []
        node: DesignNode | None = self
        while node is not None:
            name = node.primary_name
            parts.append(f"{node.label} {name!r}" if name else node.label)
            node = node.parent
        return " → ".join(reversed(parts))

    def root(self) -> DesignNode:
        """Return the root node of the design tree."""
        node = self
        while node.parent is not None:
            node = node.parent
        return node

    def ancestors_and_self(self) -> Iterator[DesignNode]:
        """Yield this node, then each ancestor up to the root."""
        node: DesignNode | None = self
        while node is not None:
            yield node
            node = node.parent

    def iter_subtree(self) -> Iterator[DesignNode]:
        """Yield this node and every descendant, depth-first, parents first."""
        yield self
        for child in self.children:
            yield from child.iter_subtree()
