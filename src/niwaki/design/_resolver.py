"""Reference resolver — closed-world validation of lazy design references.

At push time, every :class:`~niwaki.design._node.PendingBind` recorded during
construction is resolved:

1. The target must be **declared in the design** (closed world) — forward
   references are fine because resolution happens after the whole tree is
   built.  Curated aliases may point at an *abstract* target class
   (``domain`` → ``infraDomP``); the lookup then spans every generated
   concrete subclass (``physDomP``, ``l3extDomP``, …) via
   ``TARGET_SUBCLASSES``.
2. The relationship class **and its flavor** are derived from
   ``REFERENCE_MAP`` — direct (``REFERENCE_MAP[owner][target]``, Rs attached
   under the owner) or inverse (``REFERENCE_MAP[target][owner]``, Rs attached
   under the **target**, pointing back at the owner).  The caller never needs
   to know which side owns the Rs object.
3. Construction follows the flavor: ``"name"`` relations store the target's
   name (D2 renames every ``tn*Name`` prop to the Python field ``name``);
   ``"dn"`` relations store the declared target node's DN as ``target_dn``.

``bind_dn`` references skip steps 1-2 entirely — their Rs class and raw DN
were fixed at the call site; the resolver only constructs them.

The resolver never mutates the design tree: it returns the extra Rs objects
per node, so a design can be compiled and pushed repeatedly.
"""

from __future__ import annotations

import difflib

from niwaki.design._node import BindFlavor, DesignNode, PendingBind
from niwaki.exceptions._design import (
    AmbiguousBindError,
    DuplicateDeclarationError,
    UnresolvedReferenceError,
)
from niwaki.models.base import ManagedObject

# Names are namespaced per parent in ACI, so a ``(class, name)`` pair may be
# declared more than once across a multi-tenant design (``bd("web")`` in two
# tenants).  The index keeps **every** node under its key; which one a bind
# resolves to is decided per-owner by enclosing scope (see ``_lookup_target``).
_Index = dict[str, dict[str, list[DesignNode]]]


def build_index(root: DesignNode) -> _Index:
    """Index every named node of the design by ``(ACI class, primary name)``.

    Args:
        root: Design root node.

    Returns:
        ``{aci_class: {primary_name: [node, ...]}}`` — every node sharing a
        ``(class, name)`` pair is kept; scope resolution disambiguates at
        lookup time, so a name reused across tenants is not lost here.
    """
    index: _Index = {}
    for node in root.iter_subtree():
        name = node.primary_name
        if not name:
            continue
        index.setdefault(node.aci_class, {}).setdefault(name, []).append(node)
    return index


def _common_ancestor_depth(owner: DesignNode, candidate: DesignNode) -> int:
    """Depth of the nearest ancestor shared by *owner* and *candidate*.

    ACI namespaces object names per parent DN, so a reference resolves to the
    same-named target that shares the **deepest** enclosing scope with its
    owner: a BD in tenant *a* binds tenant *a*'s VRF, not a same-named VRF in
    tenant *b*.  The score is the distance of that shared ancestor from the
    ``polUni`` root — larger means a closer, more specific scope.

    Args:
        owner: The node carrying the reference.
        candidate: A declared node matching the reference's class and name.

    Returns:
        The shared ancestor's distance from the root (``0`` = only ``polUni``
        in common), or ``-1`` when the two share no ancestor (distinct trees —
        never within one design, but keeps the ordering total).
    """
    depth_of: dict[int, int] = {
        id(node): depth for depth, node in enumerate(reversed(list(owner.ancestors_and_self())))
    }
    # candidate.ancestors_and_self() runs deepest → root, so the first hit is
    # the lowest (nearest) common ancestor.
    for node in candidate.ancestors_and_self():
        hit = depth_of.get(id(node))
        if hit is not None:
            return hit
    return -1


def _target_classes(target_aci_class: str) -> list[str]:
    """Concrete classes an alias target may match (abstract → subclasses)."""
    from niwaki.domain._child_map import TARGET_SUBCLASSES

    return [target_aci_class, *TARGET_SUBCLASSES.get(target_aci_class, ())]


def _flavor_of(rs_aci_class: str) -> BindFlavor:
    """How a relationship class points at its target: by DN, or by name.

    A curated verb fixes its Rs class upfront, so the flavor is read off that
    class rather than looked up in ``REFERENCE_MAP``: a relation carrying
    ``tDn`` (renamed ``target_dn``) is a DN relation, every other one names its
    target through a ``tn*Name`` prop (renamed ``name``).

    Args:
        rs_aci_class: ACI class name of the relationship, e.g. ``"fvRsProv"``.

    Returns:
        ``"dn"`` when the class carries a ``target_dn`` field, ``"name"``
        otherwise.
    """
    from niwaki.design._cursor import _load_class

    return "dn" if "target_dn" in _load_class(rs_aci_class).model_fields else "name"


def _build_rs(rs_aci_class: str, target_fields: dict[str, str], bind: PendingBind) -> ManagedObject:
    """Construct the relationship object: its target, plus any ``ref()`` fields.

    Args:
        rs_aci_class: ACI class name of the relationship.
        target_fields: How the relation points at its target — ``target_dn``
            or ``name``, per the flavor.
        bind: The pending reference; ``bind.attrs`` holds the fields the caller
            set on the relationship itself through :func:`~niwaki.design.ref`.

    Returns:
        A validated relationship instance.

    Raises:
        DesignError: A ``ref()`` attribute is not a field of the relationship
            class (a wire name, or a typo).
        ValidationError: A ``ref()`` attribute fails the field's constraints.
    """
    from niwaki.design._cursor import _load_class, _validate_attr_names

    cls = _load_class(rs_aci_class)
    if bind.attrs:
        _validate_attr_names(cls, bind.attrs)
    return cls.model_validate({**target_fields, **bind.attrs})


def _lookup_target(index: _Index, owner: DesignNode, bind: PendingBind) -> DesignNode:
    """Return the declared node for a reference, scoped to the owner.

    Every declared node matching the target class (and, for abstract aliases,
    its concrete subclasses) and name is gathered, then the one sharing the
    **nearest enclosing scope** with *owner* wins — ACI namespaces names per
    parent, so a same-named target in another tenant/domain never shadows the
    owner's own.  A tie between two equally-near candidates is a genuine
    ambiguity and fails loudly.

    Raises:
        UnresolvedReferenceError: No declared node matches (with a suggestion).
        AmbiguousBindError: Two candidates share the owner's nearest scope.
    """
    classes = _target_classes(bind.target_aci_class)
    candidates = [
        (aci_class, node)
        for aci_class in classes
        for node in index.get(aci_class, {}).get(bind.target_name, ())
    ]
    if not candidates:
        names = sorted({name for aci in classes for name in index.get(aci, {})})
        hint = difflib.get_close_matches(bind.target_name, names, n=1)
        suggestion = f" Did you mean {hint[0]!r}?" if hint else ""
        wanted = bind.target_aci_class if len(classes) == 1 else f"{'/'.join(classes[1:])}"
        raise UnresolvedReferenceError(
            f"{owner.path()}: {bind.alias}={bind.target_name!r} does not resolve — no "
            f"{wanted} named {bind.target_name!r} is declared in this design. "
            f"Declared: {', '.join(names) or 'none'}.{suggestion}"
        )

    scored = [
        (_common_ancestor_depth(owner, node), aci_class, node) for aci_class, node in candidates
    ]
    nearest_depth = max(depth for depth, _, _ in scored)
    nearest = [(aci_class, node) for depth, aci_class, node in scored if depth == nearest_depth]
    if len(nearest) > 1:
        hit = ", ".join(sorted({aci_class for aci_class, _ in nearest}))
        raise AmbiguousBindError(
            f"{owner.path()}: {bind.alias}={bind.target_name!r} matches "
            f"{len(nearest)} objects declared at the same scope ({hit}) — "
            "rename one of the targets."
        )
    return nearest[0][1]


def resolve(root: DesignNode) -> dict[DesignNode, list[ManagedObject]]:
    """Resolve every pending reference in the design (closed world).

    Args:
        root: Design root node.

    Returns:
        Mapping of node → freshly constructed Rs instances to attach under
        that node at compile time.  The design tree itself is not mutated.

    Raises:
        UnresolvedReferenceError: A target is not declared in the design.
        AmbiguousBindError: A bind edge has no Rs class in either direction,
            or its target name is declared twice.
        DuplicateDeclarationError: Two references (or a reference and an
            explicit child) collide on the same RN under the same parent.
    """
    from niwaki.domain._child_map import REFERENCE_MAP

    index = build_index(root)
    extras: dict[DesignNode, list[ManagedObject]] = {}

    for node in root.iter_subtree():
        for bind in node.binds:
            if bind.kind == "bind_dn":
                # Rs class and raw DN fixed at the call site — no lookup.
                rs_mo = _build_rs(bind.rs_aci_class, {"target_dn": bind.target_name}, bind)
                extras.setdefault(node, []).append(rs_mo)
                continue

            target = _lookup_target(index, node, bind)

            if bind.rs_aci_class:  # curated verb — the Rs class is fixed upfront
                attach, rs_aci_class = node, bind.rs_aci_class
                flavor = _flavor_of(rs_aci_class)
            elif entry := REFERENCE_MAP.get(node.aci_class, {}).get(target.aci_class):
                attach, (rs_aci_class, flavor) = node, entry
            elif entry := REFERENCE_MAP.get(target.aci_class, {}).get(node.aci_class):
                # Inverse edge: the Rs lives on the target, pointing back here.
                attach, (rs_aci_class, flavor) = target, entry
            else:
                raise AmbiguousBindError(
                    f"{node.path()}: no unambiguous Rs class exists between "
                    f"{node.aci_class} and {target.aci_class} in either "
                    "direction. Use .mo(RsClass, ...) to create it explicitly."
                )

            # The referenced end of the edge: the target for direct relations,
            # the owner itself for inverse ones.
            referenced = target if attach is node else node
            if flavor == "dn":
                fields = {"target_dn": referenced.dn()}
            else:
                # D2 renames every Rs target prop (tn*Name) to the Python
                # field "name" — one constructor shape for all name relations.
                fields = {"name": referenced.primary_name}
            rs_mo = _build_rs(rs_aci_class, fields, bind)
            extras.setdefault(attach, []).append(rs_mo)

    _check_rn_collisions(extras)
    return extras


def _check_rn_collisions(extras: dict[DesignNode, list[ManagedObject]]) -> None:
    """Reject duplicate RNs among resolved Rs objects and explicit children.

    Catches both a double ``bind(vrf=...)`` on the same BD (singleton Rs →
    same fixed RN) and a bind colliding with an explicit ``.mo(RsClass, ...)``
    declaration.
    """
    for node, rs_list in extras.items():
        seen: set[str] = {child.rn for child in node.children}
        for rs_mo in rs_list:
            rn = rs_mo.rn
            if rn in seen:
                raise DuplicateDeclarationError(
                    f"{node.path()}: relationship {rn!r} is declared twice "
                    "(duplicate bind on the same target class?)."
                )
            seen.add(rn)
