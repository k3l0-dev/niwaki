"""The fake's object store and the write planner that feeds it.

Objects live in a flat ``{dn: (class, attributes)}`` map — the APIC's own
model: a DN names one object, the tree is implied by DN prefixes.  Writes
are *planned* first (every object an envelope materialises, placed and
normalised) and applied only once the whole plan is known to be valid, so a
refused or failure-injected POST changes nothing — an APIC POST is atomic.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from niwaki._dn import naming_values, parent_dn, rn_of
from niwaki._json import as_list, as_mapping
from niwaki.models._wire import to_wire
from niwaki.testing._semantics import (
    DN,
    PLACEHOLDER,
    Refusal,
    check_class,
    compute_rn,
    is_known_class,
    prop,
    rn_format,
)

#: The policy universe: on every APIC, always there, never removable.
UNIVERSE = "uni"
UNIVERSE_CLASS = "polUni"

#: The ``status`` values an APIC write accepts; ``deleted`` removes the subtree.
_STATUSES = frozenset({"", "created", "modified", "created,modified", "deleted"})


@dataclass(frozen=True, slots=True)
class StoredObject:
    """One object held by a :class:`~niwaki.testing.FakeApic`, as a read-only value.

    A view is detached: it is built at the moment you ask for it, and a later
    write to the fake never changes a view already handed out.  It compares
    by value — against another view, or against one you build yourself with
    a plain ``dict`` of attributes.

    Attributes:
        dn: The object's Distinguished Name.
        aci_class: Its wire class name (``"fvBD"``).
        attributes: Its wire attributes, exactly as stored — values in the
            APIC's own spelling (a named number by its name, a bitmask in the
            fabric's order).  The ``dn`` is not repeated here.  Read-only:
            assigning an item raises ``TypeError``.

    Example::

        stored = fake.get("uni/tn-prod/BD-web")
        assert stored == StoredObject(
            dn="uni/tn-prod/BD-web", aci_class="fvBD", attributes={"name": "web"}
        )
    """

    dn: str
    aci_class: str
    attributes: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))

    def __hash__(self) -> int:
        return hash((self.dn, self.aci_class, frozenset(self.attributes.items())))


@dataclass(frozen=True, slots=True)
class Upsert:
    """Create the object at *dn*, or merge *attributes* into the existing one."""

    dn: str
    aci_class: str
    attributes: dict[str, str]


@dataclass(frozen=True, slots=True)
class Delete:
    """Remove the object at *dn* and its whole subtree."""

    dn: str


type Write = Upsert | Delete


class Store:
    """The DN-indexed object map, with the APIC's subtree semantics.

    It starts with the policy universe — a ``polUni`` at ``uni`` — as every
    controller does; the write planner refuses to delete it.
    """

    def __init__(self) -> None:
        self._objects: dict[str, tuple[str, dict[str, str]]] = {UNIVERSE: (UNIVERSE_CLASS, {})}

    def __contains__(self, dn: str) -> bool:
        return dn in self._objects

    def __len__(self) -> int:
        return len(self._objects)

    def entry(self, dn: str) -> tuple[str, Mapping[str, str]] | None:
        """``(class, attributes)`` stored at *dn* (a live read-only view), or ``None``."""
        found = self._objects.get(dn)
        return None if found is None else (found[0], MappingProxyType(found[1]))

    def view(self, dn: str) -> StoredObject | None:
        """A detached :class:`StoredObject` for *dn*, or ``None``."""
        found = self._objects.get(dn)
        return None if found is None else StoredObject(dn, found[0], found[1])

    def dns(self) -> list[str]:
        """Every stored DN, in DN order."""
        return sorted(self._objects)

    def children(self, dn: str) -> list[str]:
        """The direct children of *dn*, in DN order (bracket-aware)."""
        return sorted(d for d in self._objects if parent_dn(d) == dn)

    def subtree(self, dn: str) -> list[str]:
        """*dn* itself (when stored) and every stored descendant, in DN order."""
        return sorted(d for d in self._objects if _within(d, dn))

    def class_of(self, dn: str) -> str:
        """The class of a stored object (*dn* must be stored)."""
        return self._objects[dn][0]

    def apply(self, writes: list[Write]) -> None:
        """Apply a validated write plan, in order."""
        for write in writes:
            match write:
                case Delete(dn=dn):
                    for doomed in [d for d in self._objects if _within(d, dn)]:
                        del self._objects[doomed]
                case Upsert(dn=dn, aci_class=aci_class, attributes=attributes):
                    current = self._objects.get(dn)
                    merged = (
                        {**current[1], **attributes} if current is not None else dict(attributes)
                    )
                    self._objects[dn] = (aci_class, merged)


def _within(dn: str, root: str) -> bool:
    """Whether *dn* is *root* or lies under it — a DN prefix is not a subtree."""
    return dn == root or dn.startswith(f"{root}/")


# ── Write planning ────────────────────────────────────────────────────────────


def plan_envelope(store: Store, payload: object, url_dn: str | None) -> list[Write]:
    """Turn an APIC envelope into the writes it means, without touching the store.

    Placement of a top-level object: its explicit ``dn`` attribute when it has
    one; else, for a POST, the URL DN itself when the URL already names the
    object (staged pushes post an object at its own DN, ``uni`` + ``polUni``),
    or the URL DN plus the object's RN; for a seed (no URL), its RN from the
    root.  A child lands under its parent's DN (or at its explicit ``dn``).

    Args:
        store: The current store (consulted for class conflicts only).
        payload: The envelope, ``{"<class>": {"attributes": {...}, "children": [...]}}``.
        url_dn: The DN of the POST URL, or ``None`` for a seed.

    Returns:
        The writes, parents before children.

    Raises:
        Refusal: The envelope is malformed, names an unknown class or
            property, carries an unknown ``status``, or an object cannot be
            named.
    """
    envelope = as_mapping(payload)
    if envelope is None or not envelope:
        raise Refusal("a write body must be a non-empty APIC envelope object")
    writes: list[Write] = []
    for aci_class, body in envelope.items():
        _plan_node(store, aci_class, body, None, url_dn, writes)
    return writes


def _plan_node(
    store: Store,
    aci_class: str,
    body: object,
    parent: str | None,
    url_dn: str | None,
    writes: list[Write],
) -> None:
    content = as_mapping(body)
    if content is None:
        raise Refusal(f"the {aci_class} envelope body must be an object")
    check_class(aci_class)
    unexpected = set(content) - {"attributes", "children"}
    if unexpected:
        raise Refusal(f"the {aci_class} envelope carries unexpected key(s) {sorted(unexpected)}")
    raw_attributes = as_mapping(content.get("attributes", {}))
    if raw_attributes is None:
        raise Refusal(f"the {aci_class} attributes must be an object")
    attributes = wire_attributes(aci_class, raw_attributes)
    explicit = attributes.pop(DN, None)
    status = attributes.pop("status", "")
    if status not in _STATUSES:
        raise Refusal(f"unknown status {status!r} on a {aci_class}")
    attributes = stored_attributes(aci_class, attributes)
    dn = _place(aci_class, attributes, explicit, parent, url_dn)
    if status == "deleted":
        if dn == UNIVERSE:
            raise Refusal("the policy universe (uni) cannot be deleted")
        writes.append(Delete(dn))
        return
    existing = store.entry(dn)
    if existing is not None and existing[0] != aci_class:
        raise Refusal(f"{dn} already holds a {existing[0]}, not a {aci_class}")
    writes.append(Upsert(dn, aci_class, attributes))
    children = as_list(content.get("children", []))
    if children is None:
        raise Refusal(f"the {aci_class} children must be a list")
    for child in children:
        envelope = as_mapping(child)
        if envelope is None or len(envelope) != 1:
            raise Refusal(f"each child of {dn} must be a one-class envelope")
        ((child_class, child_body),) = envelope.items()
        _plan_node(store, child_class, child_body, dn, None, writes)


def _place(
    aci_class: str,
    attributes: dict[str, str],
    explicit: str | None,
    parent: str | None,
    url_dn: str | None,
) -> str:
    """The DN an envelope node lands at — refusing any placement that contradicts itself.

    Naming values absent from the attributes are filled in from the DN that
    names the object (its explicit ``dn``, or the URL that is the object).
    """
    if explicit:
        _check_explicit(aci_class, attributes, explicit, parent, url_dn)
        return explicit
    rn = compute_rn(aci_class, attributes)
    if parent is None and url_dn is not None:
        url_rn = rn_of(url_dn)
        naming = _fits(aci_class, url_rn)
        if naming is not None:
            # The URL names an object of this very class: the body must be it.
            if rn is None:
                for name, value in naming.items():
                    attributes.setdefault(name, value)
                return url_dn
            if rn != url_rn:
                raise Refusal(
                    f"a {aci_class} whose naming computes {rn!r} was posted at {url_dn}, "
                    f"which names another {aci_class}"
                )
            return url_dn
    if rn is None:
        raise Refusal(
            f"cannot name a {aci_class}: its RN format {rn_format(aci_class)!r} needs a "
            "naming property the envelope does not carry"
        )
    if parent is not None:
        return f"{parent}/{rn}"
    if url_dn is None:
        return rn
    return f"{url_dn}/{rn}"


def _check_explicit(
    aci_class: str,
    attributes: dict[str, str],
    explicit: str,
    parent: str | None,
    url_dn: str | None,
) -> None:
    """An explicit ``dn`` must agree with the URL, the parent and the naming values."""
    if parent is not None and parent_dn(explicit) != parent:
        raise Refusal(
            f"the {aci_class} dn {explicit!r} is not directly under its parent {parent!r}"
        )
    if parent is None and url_dn is not None and not _within(explicit, url_dn):
        raise Refusal(
            f"the {aci_class} dn {explicit!r} is neither the URL DN {url_dn!r} nor under it"
        )
    rn = rn_of(explicit)
    fmt = rn_format(aci_class)
    try:
        naming = naming_values(rn, fmt)
    except ValueError:
        raise Refusal(
            f"the dn {explicit!r} does not fit the {aci_class} RN format {fmt!r}"
        ) from None
    for name, value in naming.items():
        attributes.setdefault(name, value)
    computed = compute_rn(aci_class, attributes)
    if computed != rn:
        raise Refusal(
            f"the {aci_class} naming properties compute the RN {computed!r}, "
            f"but its dn {explicit!r} ends in {rn!r}"
        )


def _fits(aci_class: str, rn: str) -> dict[str, str] | None:
    """The naming values *rn* carries if it is an RN of *aci_class*, else ``None``.

    A format made of placeholders only (no literal text) cannot tell a class
    apart, so it never claims an RN.
    """
    fmt = rn_format(aci_class)
    if not PLACEHOLDER.sub("", fmt):
        return None
    try:
        return naming_values(rn, fmt)
    except ValueError:
        return None


def wire_attributes(aci_class: str, raw: Mapping[str, object]) -> dict[str, str]:
    """Attribute values as the wire strings the APIC speaks.

    Raises:
        Refusal: A value is not a JSON scalar.
    """
    out: dict[str, str] = {}
    for name, value in raw.items():
        if isinstance(value, str):
            out[name] = value
        elif isinstance(value, bool | int | float):
            out[name] = to_wire(value)
        else:
            raise Refusal(f"{aci_class}.{name} must be a string, not {type(value).__name__}")
    return out


def stored_attributes(aci_class: str, attributes: Mapping[str, str]) -> dict[str, str]:
    """The attributes in the form the fabric stores them (named numbers, bitmask order).

    A class unknown to the catalogue keeps its attributes verbatim (it can
    only be placed by an explicit DN); a property unknown to a known class is
    refused, as the APIC refuses it.

    Raises:
        Refusal: A property is unknown to the class.
    """
    if not is_known_class(aci_class):
        return dict(attributes)
    return {name: prop(aci_class, name).stored(value) for name, value in attributes.items()}


def snapshot_writes(scope: str, tree: object) -> list[Write]:
    """The writes that rebuild a snapshot tree at its DNs (scope DN + the RN chain).

    Raises:
        Refusal: The tree is malformed.
    """
    writes: list[Write] = []

    def walk(node: object, dn: str) -> None:
        content = as_mapping(node)
        if content is None:
            raise Refusal(f"malformed snapshot node at {dn}")
        aci_class = content.get("class")
        attributes = as_mapping(content.get("attributes"))
        children = as_list(content.get("children", []))
        if not isinstance(aci_class, str) or attributes is None or children is None:
            raise Refusal(f"malformed snapshot node at {dn}")
        wire = wire_attributes(aci_class, attributes)
        writes.append(Upsert(dn, aci_class, stored_attributes(aci_class, wire)))
        for child in children:
            child_content = as_mapping(child)
            rn = child_content.get("rn") if child_content is not None else None
            if not isinstance(rn, str):
                raise Refusal(f"malformed snapshot child under {dn}")
            walk(child, f"{dn}/{rn}")

    walk(tree, scope)
    return writes
