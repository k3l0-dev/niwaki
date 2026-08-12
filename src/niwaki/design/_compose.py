"""Design composition — ``slice`` and ``merge`` (2.0 it.3 lot C).

The flagship brownfield flow needs both halves: *"import the fabric, carve
out tenant X, replay it in staging"*.  ``Cursor.slice`` carves — a fresh
design holding one subtree plus its attribute-less ancestor chain — and
:func:`merge` recombines — several designs into one, fail-loud on any
contradiction.

Both are **pure**: sources are never mutated, results are fresh designs.

Slicing and references — the wire-footprint rule
------------------------------------------------

A slice keeps exactly the references whose relationship (Rs) object lands
**inside** the sliced subtree:

- Rs inside, target inside → the bind/verb is kept as declared (the new
  design's closed world contains the target).
- Rs inside, target outside → converted so the wire stays identical without
  the target: a DN-flavored reference becomes ``bind_dn`` (the target's DN
  from the source design), a name-flavored one becomes an explicit Rs child
  carrying the exact ``tn*`` name property.
- Rs outside (an inverse edge — ``vrf.bind(l3out=…)`` materialises under
  the l3out): not part of the sliced subtree's wire at all, so it is not
  carried.  Slicing the target side keeps it.
"""

from __future__ import annotations

from typing import Any

from niwaki.design._cursor import Cursor, _attach, _load_class
from niwaki.design._node import DesignNode, PendingBind, RawDesignNode
from niwaki.design._resolver import _flavor_of, _lookup_target, build_index
from niwaki.exceptions._design import (
    DesignError,
    MergeConflictError,
    UnresolvedReferenceError,
)

# ── Shared cloning helpers ────────────────────────────────────────────────────


def _copy_bind(bind: PendingBind) -> PendingBind:
    """A private deep copy of a reference — no value is ever shared with the
    source (a mutable ``set`` in a ``ref()`` attribute must not alias)."""
    import copy
    import dataclasses

    return dataclasses.replace(bind, attrs=copy.deepcopy(bind.attrs))


def _clone_node(node: DesignNode, parent: DesignNode, *, bare: bool) -> DesignNode:
    """Clone one node under *parent* — bare (identity only) or with its state.

    Deep copies throughout: composition results share **no** mutable value
    with their sources (mutating a Flags ``set`` in a slice must never change
    the source design's wire).
    """
    import copy

    if isinstance(node, RawDesignNode):
        clone: DesignNode = RawDesignNode(
            node.wire_class,
            dict(node.wire_naming),
            node.wire_rn,
            {} if bare else dict(node.raw_attrs),
            parent,
        )
    else:
        clone = DesignNode(
            node.cls,
            node.label,
            copy.deepcopy(node.naming),
            {} if bare else copy.deepcopy(node.attrs),
            parent,
            position=node.position,
        )
        if not bare:
            clone.raw_attrs = dict(node.raw_attrs)
    parent.children.append(clone)
    return clone


def _bind_key(bind: PendingBind) -> tuple[str, ...]:
    """Hashable identity of a reference — value-stable even for ``set`` attrs."""

    def _stable(value: Any) -> str:
        if isinstance(value, (set, frozenset)):
            return f"{{{','.join(sorted(str(v) for v in value))}}}"
        return repr(value)

    return (
        bind.kind,
        bind.alias,
        bind.target_aci_class,
        bind.target_name,
        ";".join(f"{k}={_stable(v)}" for k, v in sorted(bind.attrs.items())),
    )


def _root_cursor(node: DesignNode) -> Cursor:
    """The typed root cursor (``UniCursor``) over a fresh composition result."""
    from niwaki.design._cursor import _cursor_class_for

    return _cursor_class_for("")(node)


# ── Slice ─────────────────────────────────────────────────────────────────────


def slice_design(source_root: DesignNode, dn: str) -> Cursor:
    """Carve the subtree at *dn* out of a design — see :meth:`Cursor.slice`."""
    from niwaki.design._generated_cursors import design

    # Locate the target node by DN — one walk, DNs accumulated (never O(n²)).
    dn_of: dict[int, str] = {}
    target: DesignNode | None = None
    stack: list[tuple[DesignNode, str]] = [(source_root, source_root.rn)]
    while stack:
        node, node_dn = stack.pop()
        dn_of[id(node)] = node_dn
        if node_dn == dn:
            target = node
        stack.extend((child, f"{node_dn}/{child.rn}") for child in node.children)
    if target is None:
        raise DesignError(f"slice(): the design declares nothing at {dn!r}.")

    new_root = design().design_node
    if target is source_root:
        parent = new_root
    else:
        chain = list(target.ancestors_and_self())[1:-1]  # ancestors, root excluded
        chain.reverse()
        parent = new_root
        for ancestor in chain:
            parent = _clone_node(ancestor, parent, bare=True)

    # Deep-clone the subtree (references handled in a second pass, below).
    cloned_of: dict[int, DesignNode] = {}

    def _deep(node: DesignNode, into: DesignNode) -> None:
        clone = _clone_node(node, into, bare=False)
        cloned_of[id(node)] = clone
        for child in node.children:
            _deep(child, clone)

    if target is source_root:
        # Slicing at the root copies the whole design (attrs included).
        import copy

        new_root.attrs = copy.deepcopy(source_root.attrs)
        new_root.raw_attrs = dict(source_root.raw_attrs)
        cloned_of[id(source_root)] = new_root
        for child in source_root.children:
            _deep(child, new_root)
    else:
        _deep(target, parent)

    # References: the wire-footprint rule needs EVERY bind of the source
    # examined — an inverse edge declared on an outside owner can land its
    # Rs inside the slice (vrf.bind(l3out=…) materialises under the l3out).
    index = build_index(source_root)
    inside = {id(node) for node in target.iter_subtree()}

    for node in source_root.iter_subtree():
        seen: set[tuple[str, ...]] = set()
        for bind in node.binds:
            key = _bind_key(bind)
            if key in seen:
                continue  # identical duplicates collapse, as the resolver does
            seen.add(key)
            _carry_bind(node, bind, index, inside, cloned_of, dn_of)
    return _root_cursor(new_root)


def _carry_bind(
    node: DesignNode,
    bind: PendingBind,
    index: dict[str, dict[str, list[DesignNode]]],
    inside: set[int],
    cloned_of: dict[int, DesignNode],
    dn_of: dict[int, str],
) -> None:
    """Carry one source reference into the slice — kept, converted, or dropped.

    The Rs object's **landing node** decides: inside the slice → carried
    (as the declared bind when the owner and target are both inside, pinned
    explicitly otherwise); outside → not this subtree's wire.
    """
    from niwaki.domain._child_map import REFERENCE_MAP

    owner_inside = id(node) in inside

    if bind.kind == "bind_dn":
        if owner_inside:
            cloned_of[id(node)].binds.append(_copy_bind(bind))
        return

    try:
        target = _lookup_target(index, node, bind)
    except (UnresolvedReferenceError, DesignError):
        if owner_inside:
            # Unresolvable in the SOURCE — the push would have failed there
            # too; carried as declared so the failure stays identical and loud.
            cloned_of[id(node)].binds.append(_copy_bind(bind))
        return

    # Where does the Rs materialise?
    if bind.kind == "verb" or REFERENCE_MAP.get(node.aci_class, {}).get(target.aci_class):
        landing = node  # verbs and direct edges attach under the owner
    elif REFERENCE_MAP.get(target.aci_class, {}).get(node.aci_class):
        landing = target  # inverse edge: the Rs lives under the target
    else:
        if owner_inside:
            # No Rs class either way — the push raises AmbiguousBindError in
            # the source and must do the same in the slice.
            cloned_of[id(node)].binds.append(_copy_bind(bind))
        return

    if id(landing) not in inside:
        return  # not part of this subtree's wire — the other side owns it

    if owner_inside and id(target) in inside:
        cloned_of[id(node)].binds.append(_copy_bind(bind))  # closed world intact
        return

    # The Rs lands inside but one end is missing from the slice: pin the
    # exact wire without the closed world.
    if bind.kind == "verb":
        rs_cls_name = bind.rs_aci_class
        flavor = _flavor_of(rs_cls_name)
    elif landing is node:
        rs_cls_name, flavor = REFERENCE_MAP[node.aci_class][target.aci_class]
    else:
        rs_cls_name, flavor = REFERENCE_MAP[target.aci_class][node.aci_class]

    if landing is node and flavor == "dn":
        # Direct DN edge from an inside owner: bind_dn keeps the alias.
        cloned_of[id(node)].binds.append(
            PendingBind(
                kind="bind_dn",
                alias=bind.alias,
                target_aci_class=bind.target_aci_class,
                target_name=dn_of[id(target)],
                rs_aci_class=rs_cls_name,
                flavor="dn",
                attrs=_copy_bind(bind).attrs,
            )
        )
        return

    # Referenced end of the pinned Rs: the target for a direct edge, the
    # (outside) owner for an inverse one.
    referenced = target if landing is node else node
    value = dn_of[id(referenced)] if flavor == "dn" else referenced.primary_name
    _pin_rs(cloned_of[id(landing)], rs_cls_name, flavor, value, bind.attrs)


def _pin_rs(
    clone: DesignNode,
    rs_cls_name: str,
    flavor: str,
    value: str,
    ref_attrs: dict[str, Any],
) -> None:
    """Attach the explicit Rs child a resolved reference would have emitted.

    An identical Rs already pinned (two references collapsing to one wire
    object) is skipped, mirroring the resolver's own duplicate collapse.
    """
    import copy

    from niwaki.exceptions._design import DuplicateDeclarationError

    rs_cls = _load_class(rs_cls_name)
    field = "target_dn" if flavor == "dn" else "name"
    fields: dict[str, Any] = {field: value, **copy.deepcopy(ref_attrs)}
    naming = {p: fields.pop(p) for p in rs_cls._naming_props if p in fields}  # pyright: ignore[reportPrivateUsage]
    try:
        _attach(clone, rs_cls, rs_cls_name, naming, fields)
    except DuplicateDeclarationError:
        candidate = rs_cls(**naming, **fields)
        existing = next(
            (c for c in clone.children if c.aci_class == rs_cls_name and c.rn == candidate.rn),
            None,
        )
        if existing is not None and existing.mo().to_apic() == candidate.to_apic():
            return  # the same wire object, pinned twice — collapse
        raise


# ── Merge ─────────────────────────────────────────────────────────────────────


def merge(*designs: Cursor) -> Cursor:
    """Combine several designs into one — fail-loud on any contradiction.

    The union is by DN: an object declared in one source is carried whole;
    an object declared in several must agree — same class, and no attribute
    (typed or wire-channel) set to two different values.  References
    concatenate, identical duplicates collapse (the resolver already treats
    two identical relations as one).  Every contradiction across the whole
    merge is collected before raising, never first-fail.

    Args:
        *designs: Two or more design cursors (any cursor of each design —
            the whole tree is taken, like ``push``).  Sources are never
            mutated.

    Returns:
        The root :class:`~niwaki.design.Cursor` of a fresh merged design.

    Raises:
        DesignError: Fewer than two designs given.
        MergeConflictError: At least one contradiction — carries every
            conflicting ``(dn, what, values)`` triple.

    Example::

        from niwaki.design import merge, tenant

        base = tenant("prod")
        base.bd("web")
        overlay = tenant("prod")
        overlay.bd("web").set(unicast_routing=True)
        combined = merge(base, overlay)
    """
    import copy

    from niwaki.design._generated_cursors import design

    if len(designs) < 2:
        raise DesignError("merge() takes at least two designs.")

    new_root = design().design_node
    by_dn: dict[str, DesignNode] = {"uni": new_root}
    conflicts: list[tuple[str, str, tuple[object, object]]] = []

    def _wire_name(clone: DesignNode, field: str) -> str:
        info = clone.cls.model_fields.get(field)
        alias = info.serialization_alias if info else None
        return alias if isinstance(alias, str) else field

    def _merge_attrs(dn: str, clone: DesignNode, node: DesignNode) -> None:
        # Wire names bridge the two channels: a typed field and a raw_set of
        # the same property must agree, whichever channel each side used.
        typed_by_wire = {_wire_name(clone, f): f for f in clone.attrs}
        for field, value in node.attrs.items():
            wire = _wire_name(clone, field)
            if field in clone.attrs:
                if not _values_agree(clone.cls, field, clone.attrs[field], value):
                    conflicts.append((dn, field, (clone.attrs[field], value)))
            elif wire in clone.raw_attrs:
                if not _values_agree(clone.cls, field, clone.raw_attrs[wire], value):
                    conflicts.append((dn, wire, (clone.raw_attrs[wire], value)))
            else:
                clone.attrs[field] = copy.deepcopy(value)
                typed_by_wire[wire] = field
        for wire, value in node.raw_attrs.items():
            typed = typed_by_wire.get(wire)
            if typed is not None:
                if not _values_agree(clone.cls, typed, clone.attrs[typed], value):
                    conflicts.append((dn, wire, (clone.attrs[typed], value)))
            elif wire in clone.raw_attrs:
                if clone.raw_attrs[wire] != value:
                    conflicts.append((dn, wire, (clone.raw_attrs[wire], value)))
            else:
                clone.raw_attrs[wire] = value

    def _merge_binds(clone: DesignNode, node: DesignNode) -> None:
        seen = {_bind_key(b) for b in clone.binds}
        for bind in node.binds:
            key = _bind_key(bind)
            if key not in seen:
                seen.add(key)
                clone.binds.append(_copy_bind(bind))

    def _absorb(node: DesignNode, into: DesignNode, dn: str) -> None:
        existing = by_dn.get(dn)
        if existing is None:
            clone = _clone_node(node, into, bare=True)
            by_dn[dn] = clone
            existing = clone
        elif existing.aci_class != node.aci_class:
            conflicts.append((dn, "class", (existing.aci_class, node.aci_class)))
            return
        _merge_attrs(dn, existing, node)
        _merge_binds(existing, node)
        for child in node.children:
            _absorb(child, existing, f"{dn}/{child.rn}")

    for source in designs:
        source_root = source.design_node.root()
        _merge_attrs("uni", new_root, source_root)
        _merge_binds(new_root, source_root)
        for child in source_root.children:
            _absorb(child, new_root, f"uni/{child.rn}")

    if conflicts:
        raise MergeConflictError(sorted(conflicts, key=lambda c: (c[0], c[1], repr(c[2]))))
    return _root_cursor(new_root)


def _values_agree(cls: type[Any], field: str, a: Any, b: Any) -> bool:
    """Whether two declarations of one field are the same wire value.

    A hand-written design holds coerced values (``True``), an imported one
    holds wire strings (``"true"``, ``"yes"``) — one value, several
    spellings.  Both sides coerce through the field's own annotation before
    comparing (the same seam ``from_apic`` reads through), with the
    named-number equality of :func:`niwaki.utils.diff._values_equal` on top.
    """
    from niwaki.models._wire import from_wire
    from niwaki.utils.diff import _values_equal

    if _values_equal(a, b):
        return True
    info = cls.model_fields.get(field) if hasattr(cls, "model_fields") else None
    if info is None:
        return False
    coerced_a = from_wire(info.annotation, a) if isinstance(a, str) else a
    coerced_b = from_wire(info.annotation, b) if isinstance(b, str) else b
    return _values_equal(coerced_a, coerced_b)
