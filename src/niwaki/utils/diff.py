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

from typing import cast

from niwaki._dn import rn_of
from niwaki.models._wire import from_wire
from niwaki.models.base import ManagedObject, _coerce_read  # pyright: ignore[reportPrivateUsage]
from niwaki.types import JsonObject


def _child_key(child: ManagedObject) -> tuple[str, ...]:
    """Return a hashable identity key for a child MO.

    ``(aci_class, *naming_prop_values)`` for generated classes, so children of
    the same class with the same name are the same object.  Catalogue-served
    children carry no generated identity — ``_aci_class`` is empty and
    ``_naming_props`` is empty on the base class — so they key on the public
    :attr:`~niwaki.models.base.ManagedObject.aci_class` (which falls back to
    the wire class the read recorded) and the RN.  Keying them all on ``("",)``
    collapsed distinct operational children onto one identity: identical trees
    failed with a cross-class TypeError, and same-class siblings diffed
    against each other's attributes.
    """
    if child._naming_props:  # pyright: ignore[reportPrivateUsage]
        return (child.aci_class, *(str(getattr(child, p, "")) for p in child._naming_props))
    # Base-class reads never populate an RN of their own; the DN the APIC
    # sent carries it (bracket-aware last segment).
    rn = child.rn or rn_of(str(child.attrs.get("dn", "")))
    return (child.aci_class, rn)


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


def _values_equal(desired: object, current: object) -> bool:
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

    The one exception is the **wire-name channel** of the design escape
    hatches (``raw()`` on a catalogue-served class, ``raw_set()`` on any
    node): every wire attribute *desired* declares there is compared too.  A
    prop the generated model types (renamed or spelled like its wire name)
    is coerced through the field's annotation; any other prop is compared in
    the space the catalogue-backed read path coerces to — ``"true"`` equals
    ``"yes"``, ``"syn,ack"`` equals ``"ack,syn"``, ``"0x10"`` equals
    ``"16"``.  That coercion follows the read path's rules, lenient ones
    included (a bitmask is compared as its member set without checking the
    members against the schema), and it does not canonicalise **named
    numbers**: the read path keeps a name (``"http"``) as the wire string,
    so ``"80"`` and ``"http"`` stay a change on this channel.  An unknown
    class or property, a class the catalogue cannot describe, or a value
    its kind cannot hold falls back to plain string equality (never an
    exception).  A wire attribute **absent from the read** is always a
    change — whatever the model default of the field it maps to: ``push``
    sends every raw attribute verbatim, and the APIC never echoes a secret
    (``raw_set(password="")`` would clear it).  Presence is known for a
    *current* built by :meth:`~niwaki.models.ManagedObject.from_apic` or
    :meth:`~niwaki.models.ManagedObject.from_event`; for one built locally,
    a typed field compares by value.  A reported change keeps the declared
    wire spelling.

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
    changed: dict[str, object] = {}

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
    # declares wire attrs outside the typed surface.  A prop the generated
    # model types is compared through the field's annotation; any other prop
    # (a catalogue-served class, or a prop the model omits) is read back as a
    # model extra and compared in the catalogue's coerced space.  A prop the
    # read did not return is a change whatever the model default says: push
    # sends every raw attr verbatim, and the APIC never echoes a secret.
    raw_changed: dict[str, str] = {}
    desired_raw = desired._raw_wire_attrs  # pyright: ignore[reportPrivateUsage]
    if desired_raw:
        current_extra = current.model_extra or {}
        read_keys = current._read_wire_keys  # pyright: ignore[reportPrivateUsage]
        typed_fields = _typed_wire_fields(cls)
        wire_kinds: dict[str, str | None] | None = None
        for wire, d_val in desired_raw.items():
            c_raw: object = current._raw_wire_attrs.get(wire)  # pyright: ignore[reportPrivateUsage]
            if c_raw is None and read_keys is not None and wire not in read_keys:
                raw_changed[wire] = str(d_val)
                continue
            if c_raw is None and wire in typed_fields:
                # The wire prop is a typed field on the current side (a raw_set
                # of a prop the model DOES know — renamed or spelled like its
                # wire name, from_apic stores it in the field either way):
                # coerce the desired wire string through the field's own type
                # and compare in coerced space — wire spellings have synonyms
                # ("yes" and "true" are one bool).
                field = typed_fields[wire]
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
            if c_raw is None:
                raw_changed[wire] = str(d_val)
                continue
            if d_val == str(c_raw):
                continue
            if wire_kinds is None:
                wire_kinds = _wire_kinds(desired.aci_class)
            if not _raw_values_equal(d_val, c_raw, wire_kinds.get(wire)):
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
        naming_extras: JsonObject = {
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


def _typed_wire_fields(cls: type[ManagedObject]) -> dict[str, str]:
    """``{wire name: python field}`` for every typed field of a generated class.

    The alias map lists renamed fields only (``arpFlood`` → ``arp_flooding``);
    a field whose wire name IS its Python name (``type``, ``mode``, ``port``)
    carries no alias but is just as typed — ``from_apic`` stores its value in
    the field, never in the extras.  Both kinds are returned; the bare
    :class:`~niwaki.models.base.ManagedObject` (catalogue-served) has none.
    """
    if cls is ManagedObject:
        return {}
    fields = {
        name: name
        for name, info in cls.model_fields.items()
        if name != "children" and not isinstance(info.validation_alias, str)
    }
    fields.update(cls._get_alias_map())  # pyright: ignore[reportPrivateUsage]
    return fields


def _wire_kinds(aci_class: str) -> dict[str, str | None]:
    """The catalogue's ``{wire prop: kind}`` for *aci_class* (empty when unknown).

    Empty too when the catalogue refuses to describe the class (a
    ``ValueError`` — two wire props colliding on one readable name): the raw
    compare then falls back to plain strings.
    """
    # Function-local import: the catalogue stays off ``import niwaki``'s path.
    from niwaki.query._catalog import catalog

    try:
        return catalog().class_meta(aci_class).wire_to_kind
    except (KeyError, FileNotFoundError, ValueError):
        return {}


def _raw_values_equal(desired: str, current: object, kind: str | None) -> bool:
    """Wire-string equality, widened to the catalogue's coerced space.

    Both sides go through the coercion the catalogue-backed read path applies
    (:func:`~niwaki.models.base._coerce_read` — the same coercers as the wire
    boundary): ``"true"``/``"yes"`` are one bool, ``"syn,ack"``/``"ack,syn"``
    one bitmask, ``"0x10"``/``"16"`` one integer.  With no known *kind* (an
    unknown class or property) — or a value the kind refuses — the plain
    string comparison stands: a spelling that cannot be proven equal is a
    change, never an exception.
    """
    if desired == str(current):
        return True
    if kind is None:
        return False
    try:
        return _values_equal(_coerce_read(desired, kind), _coerce_read(str(current), kind))
    except (ValueError, TypeError):
        return False


def _wire_naming(mo: ManagedObject) -> frozenset[str]:
    """Wire naming props of a catalogue-served instance (empty when unknown or refused)."""
    from niwaki.query._catalog import catalog

    try:
        return catalog().class_meta(mo.aci_class).naming
    except (KeyError, FileNotFoundError, ValueError):
        return frozenset()
