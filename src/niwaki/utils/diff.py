"""ManagedObject diff utility for surgical APIC updates.

:func:`mo_diff` compares a *desired* state object against a *current* state
object (typically retrieved from the APIC via :meth:`~niwaki.models.ManagedObject.from_apic`)
and returns a new instance carrying only the changed fields.

The returned object's ``model_fields_set`` contains exclusively the naming
props plus the fields that actually differ, so calling ``to_apic()`` on it
produces a surgical PATCH payload — the APIC leaves untouched fields at their
current values.

Children are diffed recursively by default (see ``recurse_children``):
matched by ``(_aci_class, naming prop values)`` identity.  Children present
in *desired* but absent in *current* are included as-is (additions).  Children
present only in *current* are ignored — ``mo_diff`` does not produce DELETE
ops; handle removals explicitly via the facade.

Example::

    from niwaki.models._generated.fv.fvBD import fvBD
    from niwaki.utils.diff import mo_diff

    desired = fvBD(name="web", unicast_routing=False, arp_flooding=True)
    current = fvBD.model_validate({"name": "web", "unicastRoute": "yes", "arpFlood": "no"})

    delta = mo_diff(desired, current)
    if delta:
        delta.to_apic()
        # → {"fvBD": {"attributes": {"name": "web",
        #                            "unicastRoute": "false",
        #                            "arpFlood": "true"}}}
"""

from __future__ import annotations

from typing import Any, cast

from niwaki.models._wire import from_wire
from niwaki.models.base import ManagedObject


def _child_key(child: ManagedObject) -> tuple[str, ...]:
    """Return a hashable identity key for a child MO.

    The key is ``(aci_class, *naming_prop_values)`` so that children of
    the same class with the same name are considered the same object.
    """
    return (child._aci_class, *(str(getattr(child, p, "")) for p in child._naming_props))  # pyright: ignore[reportPrivateUsage]


def _diff_children(
    desired_children: list[ManagedObject],
    current_children: list[ManagedObject],
    *,
    respect_fields_set: bool = False,
) -> list[ManagedObject]:
    """Compute the list of child deltas to embed in a parent delta.

    Args:
        desired_children: Children from the desired object.
        current_children: Children from the current (APIC) object.

    Returns:
        List of child deltas — each is either a full desired child (new) or
        a recursive :func:`mo_diff` result (changed).  Unchanged children and
        children present only in *current* are omitted.
    """
    current_index: dict[tuple[str, ...], ManagedObject] = {
        _child_key(c): c for c in current_children
    }

    deltas: list[ManagedObject] = []
    for desired_child in desired_children:
        key = _child_key(desired_child)
        if key not in current_index:
            # New child — include in full
            deltas.append(desired_child)
        else:
            child_delta = mo_diff(
                desired_child,
                current_index[key],
                respect_fields_set=respect_fields_set,
            )
            if child_delta is not None:
                deltas.append(child_delta)

    return deltas


def _values_equal(desired: Any, current: Any) -> bool:
    """Field equality.

    This used to carry a workaround: the APIC canonicalises numbers on write
    (``"80.0"`` reads back ``"80.000000"``), and while the SDK typed those fields
    as *strings* the two spelled the same value differently — every float-carrying
    design was non-idempotent under ``plan``, so the comparison had to reparse
    both sides as floats and hope.

    Numbers are numbers now, and sets are sets: ``80.0 == 80.0`` and
    ``{public, shared} == {shared, public}`` without anyone's help.  The
    workaround is gone, and equality means equality again.

    One asymmetry remains, structural to **named numbers whose names are
    numeric strings** (``poeIfPol.maximum_power: int | Literal['30000', …]``):
    construction keeps the Literal string (an exact match beats the int
    branch), while a read coerces the same wire value to ``int`` — so
    ``'30000'`` and ``30000`` are one value spelled by two coercion paths,
    and comparing them raw reported phantom drift on every plan (measured
    live).  A string that parses to the exact same integer is therefore
    equal; ``bool`` is excluded (``True == 1``, but no named number is a
    bool).
    """
    if desired == current:
        return True
    text, number = (
        (desired, current)
        if isinstance(desired, str) and type(current) is int
        else (current, desired)
        if isinstance(current, str) and type(desired) is int
        else (None, None)
    )
    if text is not None:
        try:
            return int(text) == number
        except ValueError:
            return False
    return False


def mo_diff[T: ManagedObject](
    desired: T,
    current: T,
    *,
    recurse_children: bool = True,
    respect_fields_set: bool = False,
) -> T | None:
    """Compute the delta between a desired and current ManagedObject state.

    Only declared model fields are compared — APIC-originated read-only extra
    fields (``modTs``, ``uid``, …) stored in ``model_extra`` are ignored on
    both sides.

    Args:
        desired: Target state — the object as you want it to be.
        current: Current state — typically from :meth:`~niwaki.models.ManagedObject.from_apic`.
        recurse_children: When ``True`` (default), children are diffed
            recursively and any changed or new children are included in the
            returned delta.  Set to ``False`` to restrict the diff to scalar
            fields only (original behaviour).
        respect_fields_set: When ``True``, only fields present in
            ``desired.model_fields_set`` are compared — fields the caller
            never touched are ignored even if the current state differs from
            the schema default.  This is the mode used by the design DSL's
            ``plan`` push: a design that does not set ``unicast_routing``
            must not report a change just because the APIC value differs
            from the model default.  Default ``False`` (compare all declared
            fields — original behaviour).

    Returns:
        A new instance of the same class with ``model_fields_set`` limited to
        naming props + changed fields (+ changed children when
        ``recurse_children=True``).  Returns ``None`` when the objects are
        identical (no diff to apply).

    Raises:
        TypeError:  When ``desired`` and ``current`` are of different classes.
        ValueError: When a naming prop differs between the two objects (they
                    represent different objects and cannot be diffed).

    Example::

        desired = fvBD(name="web", unicast_routing=False)
        current = fvBD.model_validate({"name": "web", "unicastRoute": "yes"})

        delta = mo_diff(desired, current)
        # delta.to_apic() → {"fvBD": {"attributes": {"name": "web", "unicastRoute": "false"}}}

        mo_diff(desired, desired)  # → None  (no change)

    Children example::

        from niwaki.models._generated.fv.fvSubnet import fvSubnet

        desired_bd = fvBD(name="web")
        desired_bd.children = [fvSubnet(ip="10.1.0.1/24", scope="public")]

        current_bd = fvBD.model_validate({"name": "web"})
        current_bd.children = [fvSubnet(ip="10.1.0.1/24", scope="private")]

        delta = mo_diff(desired_bd, current_bd)
        # delta has one child: fvSubnet with scope="public"
    """
    if type(desired) is not type(current):
        raise TypeError(
            f"Cannot diff {type(desired).__name__!r} against"
            f" {type(current).__name__!r}: classes must match"
        )
    if desired.aci_class != current.aci_class:
        # Two catalogue-served instances of different wire classes are both
        # bare ManagedObject at the Python level - the wire class is the type.
        raise TypeError(
            f"Cannot diff wire class {desired.aci_class!r} against"
            f" {current.aci_class!r}: classes must match"
        )

    cls: type[T] = type(desired)
    naming_props = set(cls._naming_props)  # pyright: ignore[reportPrivateUsage]

    # Naming props must be identical — different values → different objects
    for prop in naming_props:
        d_val = getattr(desired, prop, None)
        c_val = getattr(current, prop, None)
        if d_val != c_val:
            raise ValueError(f"Cannot diff: naming prop {prop!r} differs ({d_val!r} vs {c_val!r})")

    # Compare all non-naming declared fields — or only the explicitly set
    # ones when respect_fields_set is requested.  Write-only props (passwords,
    # pre-shared keys) are excluded: the APIC never echoes them on reads, so
    # comparing them would report phantom drift forever.  Consequence: a
    # changed secret is invisible to ``plan`` — pushing the design is the only
    # way to rotate it.
    model_fields = (
        set(cls.model_fields.keys()) - {"children"} - naming_props - cls._secure_props  # pyright: ignore[reportPrivateUsage]
    )
    if respect_fields_set:
        model_fields &= desired.model_fields_set
    changed: dict[str, Any] = {}

    for field in model_fields:
        d_val = getattr(desired, field, None)
        c_val = getattr(current, field, None)
        # Mirror ``to_apic()``: an empty string on a non-naming field is dropped
        # from the POST (it would clobber the APIC value), so ``push`` never
        # sends it.  Reporting it as a change would make ``plan`` promise an
        # update ``push`` never makes — and the drift would never converge.
        if d_val == "":
            continue
        if not _values_equal(d_val, c_val):
            changed[field] = d_val

    # Wire-name escape hatch (design .raw()/.raw_set()): the desired side
    # declares wire attrs outside the typed surface; the current side (a
    # from_apic read) holds them as model extras.  Compared as wire strings.
    raw_changed: dict[str, str] = {}
    current_extra = current.model_extra or {}
    alias_map = cls._get_alias_map() if cls is not ManagedObject else {}  # pyright: ignore[reportPrivateUsage]
    for wire, d_val in desired._raw_wire_attrs.items():  # pyright: ignore[reportPrivateUsage]
        c_raw: Any = current._raw_wire_attrs.get(wire)  # pyright: ignore[reportPrivateUsage]
        if c_raw is None and wire in alias_map:
            # The wire prop is a typed field on the current side (a raw_set of
            # a prop the model DOES know): coerce the desired wire string
            # through the field's own type and compare in coerced space —
            # wire spellings have synonyms ("yes" and "true" are one bool).
            field = alias_map[wire]
            field_val = getattr(current, field, None)
            try:
                coerced = from_wire(cls.model_fields[field].annotation, d_val)
            except (ValueError, TypeError):
                coerced = d_val
            if not _values_equal(coerced, field_val):
                raw_changed[wire] = str(d_val)
            continue
        if c_raw is None:
            c_raw = current_extra.get(wire)
        if c_raw is None or str(d_val) != str(c_raw):
            raw_changed[wire] = str(d_val)

    # Compute child deltas when requested
    child_deltas: list[ManagedObject] = []
    if recurse_children:
        child_deltas = _diff_children(
            desired.children,
            current.children,
            respect_fields_set=respect_fields_set,
        )

    if not changed and not raw_changed and not child_deltas:
        return None

    # Build the delta instance: naming props + changed fields only.
    naming = {prop: getattr(desired, prop) for prop in naming_props}
    if cls is ManagedObject:
        # Catalogue-served wire pair: rebuild the delta through the tolerant
        # read path so the wire class and naming extras survive, then carry
        # the changed raw attrs on the wire-attr channel.
        naming_extras = {
            k: str(v) for k, v in (desired.model_extra or {}).items() if k in _wire_naming(desired)
        }
        delta = cast(
            "T", ManagedObject.from_apic({desired.aci_class: {"attributes": naming_extras}})
        )
        delta._raw_wire_attrs = raw_changed  # pyright: ignore[reportPrivateUsage]
        delta._raw_rn = desired.rn  # pyright: ignore[reportPrivateUsage]
    else:
        delta = cls.surgical(naming, **changed)
        if raw_changed:
            delta._raw_wire_attrs = raw_changed  # pyright: ignore[reportPrivateUsage]
    if child_deltas:
        delta.children.extend(child_deltas)
    return delta


def _wire_naming(mo: ManagedObject) -> frozenset[str]:
    """Wire naming props of a catalogue-served instance (empty when unknown)."""
    from niwaki.query._catalog import catalog

    try:
        return catalog().class_meta(mo.aci_class).naming
    except (KeyError, FileNotFoundError):
        return frozenset()
