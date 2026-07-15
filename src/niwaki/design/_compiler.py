"""Design compilers — pure functions from a resolved design to push inputs.

Three targets, one per push mode:

- :func:`compile_poluni` → the design root *is* a ``polUni`` node, so the
  strict envelope is simply its recursive serialisation;
- :func:`compile_ops` → flat op list for wave execution (``staged``) — the
  op unit lives in :mod:`niwaki.design._engine` and never appears in the
  DSL's public results;
- :func:`build_desired_tree` → a fully-nested ManagedObject tree to diff
  against the current APIC state (``plan``).

All functions take the ``extras`` mapping produced by
:func:`niwaki.design._resolver.resolve` and never mutate the design tree,
so compilation is repeatable.
"""

from __future__ import annotations

from typing import Any

from niwaki.design._engine import _Op
from niwaki.design._node import DesignNode
from niwaki.models.base import ManagedObject

_Extras = dict[DesignNode, list[ManagedObject]]


def compile_envelope(node: DesignNode, extras: _Extras) -> dict[str, Any]:
    """Serialise *node* and its subtree into one nested APIC envelope.

    Args:
        node: Subtree root.
        extras: Resolved Rs objects per node (from the resolver).

    Returns:
        APIC envelope dict with recursively nested ``children``.
    """
    envelope = node.mo().to_apic()
    children: list[dict[str, Any]] = [compile_envelope(c, extras) for c in node.children]
    children.extend(rs.to_apic() for rs in extras.get(node, []))
    if children:
        envelope[node.aci_class]["children"] = children
    return envelope


def compile_poluni(root: DesignNode, extras: _Extras) -> dict[str, Any]:
    """Serialise the design (rooted on ``polUni``) for one atomic POST."""
    return compile_envelope(root, extras)


def compile_ops(root: DesignNode, extras: _Extras) -> list[_Op]:
    """Flatten the design into per-object POST ops (payloads without children).

    DNs are derived from the parent chain (``uni/<rn>/<rn>/…``); the
    engine's DN-depth toposort then guarantees parents-before-children
    execution.

    Classes curated as ``atomic`` in the vocabulary (``fabricExplicitGEp``)
    are the exception: the APIC validates their subtree as a whole (a vPC
    pair must arrive with both of its node endpoints), so one op ships the
    fully-nested envelope and the children emit no separate ops.

    Returns:
        One op per declared object plus one per resolved Rs.
    """
    from niwaki.design._cursor import _tables

    tables = _tables()
    atomic, carrier = tables.atomic, tables.carrier
    ops: list[_Op] = []

    def _walk(node: DesignNode, parent_dn: str) -> None:
        dn = f"{parent_dn}/{node.rn}"
        if node.aci_class in atomic:
            ops.append(_Op(dn=dn, method="POST", payload=compile_envelope(node, extras)))
            return
        # A curated carrier is a plugin-managed path prefix the APIC rejects on a
        # standalone POST (a VMM provider, ``uni/vmmp-VMware``).  Emit no op — its
        # children and Rs post at their full DNs and the APIC materialises the path.
        if node.aci_class not in carrier:
            ops.append(_Op(dn=dn, method="POST", payload=node.mo().to_apic()))
        for rs in extras.get(node, []):
            ops.append(_Op(dn=f"{dn}/{rs.rn}", method="POST", payload=rs.to_apic()))
        for child in node.children:
            _walk(child, dn)

    # The polUni root itself is never an op — it always exists on the APIC.
    for child in root.children:
        _walk(child, "uni")
    return ops


def build_desired_tree(node: DesignNode, extras: _Extras) -> ManagedObject:
    """Materialise the design as a nested ManagedObject tree (for ``plan``).

    Fresh instances are constructed on every call — the design tree and any
    user-held objects are never mutated.

    Returns:
        The subtree root MO with ``children`` recursively populated.
    """
    mo = node.mo()
    mo.children.extend(build_desired_tree(c, extras) for c in node.children)
    mo.children.extend(extras.get(node, []))
    return mo
