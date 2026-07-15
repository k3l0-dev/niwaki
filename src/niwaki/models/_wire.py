"""The wire boundary — where Python values become APIC attributes, and back.

The APIC speaks strings.  Every attribute it accepts and every attribute it
returns is a string: ``"true"``, ``"180"``, ``"public,shared"``, ``"http"``.
Python does not, and the whole point of this SDK is that it does not have to.

This module owns **both directions of that translation**, and it is the only
place that does:

``to_wire``
    Python value → the exact string the APIC expects.  Used by
    :meth:`~niwaki.models.base.ManagedObject.to_apic` (every POST the SDK ever
    sends) and by RN computation (a naming value must reach the wire in the
    form the APIC will store, or the DN we compute is not the DN that exists).

``from_wire``
    APIC string → the declared Python type.  Used by
    :meth:`~niwaki.models.base.ManagedObject.from_apic`, which builds instances
    with ``model_construct`` — no validators run, so nothing else would coerce
    the value.  A field whose read-back value keeps the wire's type would drift
    forever against its declared counterpart in ``push(mode="plan")``.

``to_filter``
    Python value → the string form used inside an APIC query filter.

Keeping the three in one module is deliberate: they are the same table read in
three directions, and a type the SDK learns to *write* but not to *read* is a
permanent, silent drift.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from enum import Enum
from functools import cache
from types import UnionType
from typing import Annotated, Any, Union, get_args, get_origin

# APIC truthy/falsy string representations for bool fields.
_APIC_TRUE = frozenset(("yes", "true", "1", "on"))
_APIC_FALSE = frozenset(("no", "false", "0", "off"))

# The separator the APIC uses inside a bitmask attribute ("public,shared").
_FLAG_SEP = ","


type Flags[E: Enum] = frozenset[E] | set[E] | str
"""What a bitmask field accepts — and what a type checker must be told it accepts.

A bitmask is *stored* as ``frozenset[E]``: that is what the APIC means and what
makes ``{public, shared} == {shared, public}`` true whatever order the fabric
returns them in.  But :func:`parse_flags` also accepts the wire form — and a
type checker cannot see through a ``BeforeValidator``.  Declared as the bare
``frozenset[E]``, ``vzEntry(tcp_rules="syn,ack")`` — an everyday ACI filter, and
the very example this SDK's bitmask support exists for — would be red in the
user's editor while working perfectly at runtime.

So the union states the truth about the *input*: a set of members, or the
comma-joined string the APIC itself speaks.  Nothing is loosened at runtime —
``"pubIic"`` is still a ``ValidationError``, because the string arm is
unreachable: :func:`parse_flags` has already split it into member names before
pydantic looks at the union.
"""


# ── Python → wire ─────────────────────────────────────────────────────────────


def to_wire(value: Any) -> str:
    """Render *value* the way the APIC stores it.

    Args:
        value: A validated Python field value.

    Returns:
        The APIC attribute string.

    Notes:
        A set of flags is joined in **declaration order of its enum**, which the
        generator emits in ascending bit weight — the order the APIC itself uses
        (``lacpLagPol.ctrl`` defaults to ``"susp-individual,graceful-conv,
        fast-sel-hot-stdby"``, i.e. weights 1, 2, 8).  Comparison never depends
        on this — a flags field is a set on both sides — but a stable order keeps
        payloads deterministic.

    Example:
        >>> to_wire(True)
        'true'
        >>> to_wire(180)
        '180'
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, frozenset | set):
        return _FLAG_SEP.join(_canonical_order(value))
    return str(value)


def _canonical_order(members: Iterable[Any]) -> list[str]:
    """Order flag members by their enum's declaration (ascending bit weight)."""
    values = list(members)
    if not values:
        return []
    first = values[0]
    if isinstance(first, Enum):
        order = list(type(first))
        return [str(m.value) for m in sorted(values, key=order.index)]
    return sorted(str(v) for v in values)


def to_filter(value: Any) -> str:
    """Render *value* for an APIC query filter.

    The filter grammar has its own booleans (``yes``/``no``); everything else
    reaches it exactly as it is stored.

    Args:
        value: A Python value used in a ``where()`` clause.

    Returns:
        The string to embed in the filter expression.
    """
    if isinstance(value, bool):
        return "yes" if value else "no"
    return to_wire(value)


def parse_flags(value: Any) -> Any:
    """Accept every reasonable spelling of a set of flags (a ``BeforeValidator``).

    Generated models declare a bitmask as ``frozenset[SomeEnum]``, and Pydantic
    would otherwise refuse the very form the APIC uses — a comma-joined string.
    This lets all of these mean the same thing::

        fvSubnet(scope="public,shared")            # the wire form
        fvSubnet(scope={"public", "shared"})       # a set of names
        fvSubnet(scope={RouteScp.PUBLIC, ...})     # a set of members
        fvSubnet(scope="private")                  # a single flag
        fvSubnet(scope="")                         # no flags at all

    Args:
        value: Whatever the caller (or the APIC) supplied.

    Returns:
        A list of member names Pydantic can validate against the enum, or the
        value untouched when it is not a string — a set, a list and a lone
        member all validate on their own.
    """
    if isinstance(value, str):
        return [part for part in (piece.strip() for piece in value.split(_FLAG_SEP)) if part]
    return value


def named_number(aliases: Mapping[str, str]) -> Callable[[Any], Any]:
    """Build the validator that stores a number under the name the APIC gives it.

    592 numeric properties in the ACI schemas declare *named* values, and the
    APIC canonicalises to the name: a filter port written as ``80`` comes back as
    ``"http"``, a BGP stale interval of ``300`` as ``"default"``, an unset port
    as ``"unspecified"``.  All three were measured on a 6.0(9c) fabric.

    A model that kept the number would compare ``80`` against ``"http"`` for
    ever — the object never converges in ``plan``, and where the property is a
    *naming* one (35 of them, including filter ports), the DN the SDK computes is
    not the DN that exists: ``push`` would create a second object beside the
    first.

    So the name is stored, not the number, and both sides of every comparison
    speak the APIC's language.

    Args:
        aliases: Number → name, as the schema declares it (``{"80": "http"}``).

    Returns:
        An ``AfterValidator`` callable: a number with a name becomes the name;
        everything else passes through untouched.
    """

    def canonicalise(value: Any) -> Any:
        if isinstance(value, int | float) and not isinstance(value, bool):
            return aliases.get(_number_key(value), value)
        return value

    return canonicalise


def _number_key(value: int | float) -> str:
    """The key a number would have in the schema's alias table (``80`` → ``"80"``)."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


# ── Wire → Python ─────────────────────────────────────────────────────────────


def from_wire(annotation: Any, value: Any) -> Any:
    """Coerce a raw APIC attribute onto its declared Python type.

    ``from_apic`` builds instances through ``model_construct``, which runs no
    validator — so this is the only thing standing between the wire's strings
    and a field's declared type.  A kind it does not know keeps the wire type
    and drifts against its own declaration for ever, which is why the
    per-property guard suite re-derives every field from the raw schemas.

    Args:
        annotation: The field's Pydantic annotation (possibly ``Annotated``).
        value: The raw value from the APIC attributes dict.

    Returns:
        The coerced value — or *value* untouched when the annotation is one this
        SDK deliberately keeps as a string (a DN, an encap, a password), or when
        the APIC sent something the type cannot hold (the APIC is trusted: a
        surprising read is never an exception).
    """
    if value is None:
        return value
    coerce = _coercer(_unwrap(annotation))
    return coerce(value)


def _unwrap(annotation: Any) -> Any:
    """Strip ``Annotated[X, ...]`` down to ``X``, and ``Flags[E]`` down to ``frozenset[E]``.

    ``Flags`` is a PEP 695 alias, so ``get_origin(Flags[E])`` is the alias itself
    — not ``frozenset``.  Left unresolved, a bitmask read back from the APIC
    would keep the wire's string ("public,shared") against a declared set, and
    drift against its own declaration on every plan, for ever.
    """
    while True:
        if get_origin(annotation) is Annotated:
            annotation = get_args(annotation)[0]
            continue
        if get_origin(annotation) is Flags:
            (member,) = get_args(annotation)
            return frozenset[member]  # type: ignore[valid-type]
        return annotation


@cache
def _coercer(annotation: Any) -> Any:
    """Build (once per annotation) the callable that coerces a wire value."""
    origin = get_origin(annotation)

    if annotation is bool:
        return _to_bool
    if annotation is int:
        return _to_int
    if annotation is float:
        return _to_float

    # A single-choice enum.  Without this, an object read from the APIC keeps
    # plain strings in its enum fields — they compare equal (StrEnum), but the
    # declared type is a lie and pydantic warns on every model_dump.
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return lambda value: _to_enum(annotation, value)

    # frozenset[SomeFlagEnum] — a bitmask.
    if origin in (frozenset, set):
        (member,) = get_args(annotation) or (str,)
        return lambda value: _to_flags(member, value)

    # int | Literal["unspecified", ...] — a number the APIC may name.  The APIC
    # canonicalises: a value with a name is stored under that name (port 80 is
    # read back as "http", a BGP stale interval of 300 as "default"), so a
    # keyword arriving here is the normal case, not the exception.
    #
    # The numeric arm carries its own bounds, so it arrives as
    # ``Annotated[int, Field(ge=…, le=…)]`` — unwrap every member before looking,
    # or the union is silently mistaken for a plain string and the APIC's "8080"
    # never becomes 8080.  That is drift, on every numeric field, for ever.
    if origin in (Union, UnionType):
        members = {_unwrap(arg) for arg in get_args(annotation)}
        if int in members:
            return _to_int
        if float in members:
            return _to_float

    return _identity


def _to_enum(enum_cls: Any, value: Any) -> Any:
    """Coerce onto an enum member; an unknown value is handed back untouched."""
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(value)
    except ValueError:
        return value


def _identity(value: Any) -> Any:
    return value


def _to_bool(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    text = str(value).lower()
    if text in _APIC_TRUE:
        return True
    if text in _APIC_FALSE:
        return False
    return value


def _to_int(value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, str):
        return value
    try:
        return int(value, 0) if value.lower().startswith("0x") else int(value)
    except ValueError:
        return value


def _to_float(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return float(value)
    except ValueError:
        return value


def _to_flags(member: Any, value: Any) -> Any:
    """Parse ``"public,shared"`` into a set of enum members.

    An unknown member is kept as-is (the APIC is trusted); the whole value is
    returned untouched only when nothing about it parses, so a surprising read
    never raises.
    """
    if isinstance(value, frozenset | set):
        return frozenset(value)
    if not isinstance(value, str):
        return value
    parts = [part for part in (p.strip() for p in value.split(_FLAG_SEP)) if part]
    if not parts:
        return frozenset()
    if isinstance(member, type) and issubclass(member, Enum):
        out = []
        for part in parts:
            try:
                out.append(member(part))
            except ValueError:
                return value  # not a member set at all — hand the raw value back
        return frozenset(out)
    return frozenset(parts)


__all__ = ["Flags", "from_wire", "named_number", "parse_flags", "to_filter", "to_wire"]
