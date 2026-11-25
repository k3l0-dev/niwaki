"""What the fake knows about ACI classes and properties — read, never re-invented.

The fake answers like an APIC only as far as the SDK itself knows the APIC:
naming (RN formats), property kinds (number, bitmask, boolean), named
numbers, configurable and naming flags.  Every one of those facts already
lives in the SDK — in the generated models and in the shipped read
catalogue — so this module is a thin, memoised reader over them, plus the
comparison rules the fabric was measured to apply (a named number matches
by value, a bitmask as a set, a boolean whatever its spelling).

Nothing here is public: :mod:`niwaki.testing` exposes the fake, not its
schema plumbing.
"""

from __future__ import annotations

import importlib
import inspect
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, cast

from niwaki import catalog
from niwaki.exceptions._query import UnknownClassError
from niwaki.models._wire import NamedRegister, from_wire, split_flags, to_wire

if TYPE_CHECKING:
    from pydantic.fields import FieldInfo

    from niwaki.models.base import ManagedObject

#: The fixed APIC property every object carries; never a stored attribute.
DN = "dn"

#: A ``{prop}`` placeholder of an RN format.
PLACEHOLDER = re.compile(r"\{([^}]+)\}")

#: Kinds whose fabric order the fake does not know: no ``lt``/``gt``/``bw``/``order-by``.
_UNORDERED = frozenset({"enum", "ip", "mac", "flags"})
#: Properties on which the APIC refuses any wildcard (measured on 6.0(9c): HTTP 301).
_NO_WILDCARD_PROPS = frozenset({DN, "descr"})


class Refusal(Exception):
    """A request — or a seed — the fake cannot answer faithfully.

    Raised anywhere in the fake's machinery and turned, at the HTTP boundary,
    into an APIC error envelope whose text starts with ``"FakeApic:"``.  The
    message names the offending piece (parameter, operator, class, property).
    """


# ── Classes ──────────────────────────────────────────────────────────────────


def check_class(aci_class: str) -> None:
    """Fail loud on a class the shipped catalogue does not know.

    Args:
        aci_class: A wire class name.

    Raises:
        Refusal: The catalogue has no such class.
    """
    _class_known(aci_class)


@cache
def _class_known(aci_class: str) -> None:
    try:
        catalog.class_meta(aci_class)
    except UnknownClassError:
        raise Refusal(f"unknown class {aci_class!r}") from None


def is_known_class(aci_class: str) -> bool:
    """Whether the shipped catalogue knows *aci_class*."""
    try:
        _class_known(aci_class)
    except Refusal:
        return False
    return True


@cache
def expand_class(aci_class: str) -> frozenset[str]:
    """The concrete classes a class name stands for in a query.

    A query on an abstract class (``fvEPg``) fans out to every concrete
    descendant (``fvAEPg``, ``fvESg``…), exactly as the APIC does server-side.

    Args:
        aci_class: A wire class name.

    Returns:
        The class itself plus every concrete descendant.

    Raises:
        Refusal: The class is unknown.
    """
    check_class(aci_class)
    return frozenset({aci_class, *catalog.concrete_subclasses(aci_class)})


@cache
def model_class(aci_class: str) -> type[ManagedObject] | None:
    """The generated model of a class, imported on demand — ``None`` when there is none."""
    from niwaki.models._generated import _PKG_MAP  # pyright: ignore[reportPrivateUsage]
    from niwaki.models.base import REGISTRY

    pkg = _PKG_MAP.get(aci_class)
    if pkg is None:
        return None
    importlib.import_module(f"niwaki.models._generated.{pkg}.{aci_class}")
    return REGISTRY.get(aci_class)


@cache
def naming_props(aci_class: str) -> frozenset[str]:
    """Wire names of a class's naming properties (empty for an unknown class)."""
    if not is_known_class(aci_class):
        return frozenset()
    return catalog.class_meta(aci_class).naming


@cache
def configurable_props(aci_class: str) -> frozenset[str] | None:
    """Wire names the catalogue marks configurable — ``None`` for an unknown class."""
    if not is_known_class(aci_class):
        return None
    return frozenset(
        wire for wire, flags in catalog.prop_flags(aci_class).items() if flags.is_configurable
    )


# ── Relative names ───────────────────────────────────────────────────────────


@cache
def rn_format(aci_class: str) -> str:
    """The class's RN format, from the catalogue.

    Raises:
        Refusal: The class is unknown, or names no RN format.
    """
    check_class(aci_class)
    fmt = catalog.rn_format(aci_class)
    if not fmt:
        raise Refusal(f"class {aci_class!r} has no RN format: it cannot be placed by name")
    return fmt


def compute_rn(aci_class: str, attributes: Mapping[str, str]) -> str | None:
    """The RN an object of *aci_class* with these wire attributes takes.

    The generated model computes it for a generated class (the same code the
    SDK uses to name what it pushes); the catalogue's RN format is filled for
    a catalogue-only class.

    Args:
        aci_class: The wire class name.
        attributes: The object's wire attributes (already in stored form).

    Returns:
        The RN, or ``None`` when a naming property the format needs is absent.

    Raises:
        Refusal: The class is unknown or has no RN format.
    """
    fmt = rn_format(aci_class)
    if any(name not in attributes for name in PLACEHOLDER.findall(fmt)):
        return None
    model = model_class(aci_class)
    if model is not None:
        from niwaki.models.base import ManagedObject

        envelope: dict[str, object] = {aci_class: {"attributes": dict(attributes)}}
        return ManagedObject.from_apic(envelope).rn
    return PLACEHOLDER.sub(lambda match: attributes[match.group(1)], fmt)


# ── Properties ───────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Prop:
    """How one property of one class stores, compares and sorts.

    Attributes:
        aci_class: The class the property belongs to.
        wire: The wire property name.
        kind: The catalogue's coercion kind (``"int"``, ``"flags"``…), or
            ``None`` when the property reads as a plain string.
        default: The schema default, in wire form — what the APIC answers for a
            property nobody wrote.
        aliases: For a named number, number → name (``{"80": "http"}``).
        flag_order: For a catalogue-only bitmask, the members in schema order.
        field: The generated model's field, when the class has a model.
        unresolved: A named number the fake has no number → name table for
            (a class with no generated model): only its names can be stored
            and compared, never a number.
    """

    aci_class: str
    wire: str
    kind: str | None
    default: str
    aliases: Mapping[str, str] | None
    flag_order: tuple[str, ...]
    field: FieldInfo | None
    unresolved: bool = False

    @property
    def qualified(self) -> str:
        """``class.prop``, as a filter names it."""
        return f"{self.aci_class}.{self.wire}"

    # ── What the fake can honour ─────────────────────────────────────────────

    def check_orderable(self, *, sorting: bool = False) -> None:
        """Refuse an ordering the fake cannot reproduce faithfully.

        Numbers order numerically and strings as text; ``order-by`` also sorts
        booleans (false first).  An enum, an address, a bitmask — or a named
        number without its table — has a fabric order the fake does not know.

        Args:
            sorting: ``True`` for ``order-by``, ``False`` for a filter comparison.

        Raises:
            Refusal: The property cannot be ordered.
        """
        if self.unresolved:
            raise Refusal(
                f"{self.qualified} is a named number whose names the fake cannot "
                "resolve to numbers: it cannot be ordered"
            )
        if self.kind in _UNORDERED or (self.kind == "bool" and not sorting):
            raise Refusal(f"{self.qualified} is a {self.kind}: the fake does not know its order")

    def check_literal(self, literal: str) -> None:
        """Refuse a numeric equality literal on an unresolvable named number."""
        if self.unresolved and _parse_number(literal) is not None:
            raise Refusal(
                f"{self.qualified} is a named number whose names the fake cannot resolve: "
                f"compare it with a name, not with the number {literal!r}"
            )

    def check_wildcard(self, pattern: str) -> None:
        """Refuse a ``wcard`` the APIC refuses (leading ``*``; ip, dn and descr)."""
        if pattern.startswith("*"):
            raise Refusal(
                f"wcard pattern {pattern!r} on {self.qualified} starts with '*': "
                "the APIC refuses a leading wildcard"
            )
        if self.kind == "ip" or self.wire in _NO_WILDCARD_PROPS:
            raise Refusal(f"wcard on {self.qualified}: the APIC refuses any wildcard there")

    # ── Storage ──────────────────────────────────────────────────────────────

    def stored(self, value: str) -> str:
        """The form the APIC stores a written value in.

        A named number is stored by its name (``"80"`` → ``"http"``); a bitmask
        is stored in an order of the fabric's choosing — the reverse of the
        SDK's canonical order, so a test can never pass on a parroted payload,
        and a fixed point (storing the stored value again changes nothing).
        Everything else is stored as written.
        """
        if self.aliases is not None:
            return self._named(value)
        if self.unresolved and _parse_number(value) is not None:
            raise Refusal(
                f"cannot store {value!r} in {self.qualified}: it is a named number "
                "the fake has no name table for — write the value's name"
            )
        if self.kind == "flags":
            members = self._canonical_flags(value)
            return ",".join(reversed(members)) if members is not None else value
        return value

    # ── Comparison ───────────────────────────────────────────────────────────

    def equal(self, stored: str, literal: str) -> bool:
        """Semantic equality, as measured on a 6.0(9c) APIC."""
        if self.kind == "flags":
            return frozenset(split_flags(stored)) == frozenset(split_flags(literal))
        if self.kind == "bool":
            left, right = from_wire(bool, stored), from_wire(bool, literal)
            if isinstance(left, bool) and isinstance(right, bool):
                return left == right
            return stored == literal
        if self.aliases is not None and self._named(stored) == self._named(literal):
            return True
        if self.kind in ("int", "float"):
            left_n, right_n = self.number(stored), self.number(literal)
            if left_n is not None and right_n is not None:
                return left_n == right_n
        return stored == literal

    def order(self, stored: str, literal: str) -> int:
        """Three-way comparison of a stored value against a filter literal.

        Returns:
            Negative, zero or positive, as ``stored`` sorts before, with or after
            ``literal``.

        Raises:
            Refusal: The property has no order the fake knows
                (:meth:`check_orderable`), or a numeric property holds a value
                the fake cannot read as a number.
        """
        self.check_orderable()
        if self.kind in ("int", "float"):
            left, right = self.number(stored), self.number(literal)
            if left is None or right is None:
                raise Refusal(
                    f"cannot compare {self.aci_class}.{self.wire} values "
                    f"{stored!r} and {literal!r} as numbers"
                )
            return (left > right) - (left < right)
        return (stored > literal) - (stored < literal)

    def sort_key(self, value: str) -> tuple[int, float, str]:
        """A total sort key for ``order-by`` — numbers numerically, the rest as text."""
        if self.kind in ("int", "float"):
            number = self.number(value)
            if number is not None:
                return (0, number, "")
        if self.kind == "bool":
            flag = from_wire(bool, value)
            if isinstance(flag, bool):
                return (0, float(flag), "")
        return (1, 0.0, value)

    def number(self, value: str) -> float | None:
        """The numeric value of *value* — a name resolved through the alias table."""
        parsed = _parse_number(value)
        if parsed is not None:
            return parsed
        if self.aliases is not None:
            for number, name in self.aliases.items():
                if name == value:
                    return _parse_number(number)
        return None

    def matches_any_bit(self, stored: str, literal: str) -> bool:
        """``anybit``: at least one of the literal's flags is set in the stored mask."""
        return bool(frozenset(split_flags(stored)) & frozenset(split_flags(literal)))

    # ── Internals ────────────────────────────────────────────────────────────

    def _named(self, value: str) -> str:
        """A named number's canonical form: the name when the number has one."""
        number = _parse_number(value)
        if number is None or self.aliases is None:
            return value
        key = str(int(number)) if number.is_integer() else str(number)
        return self.aliases.get(key, value)

    def _canonical_flags(self, value: str) -> list[str] | None:
        """The members of a bitmask in the SDK's canonical (ascending) order."""
        members = split_flags(value)
        if not members:
            return None
        if self.field is not None:
            parsed = from_wire(self.field.annotation, value)
            if isinstance(parsed, frozenset):
                return split_flags(to_wire(parsed))
        known = [member for member in self.flag_order if member in members]
        return known + sorted(member for member in members if member not in self.flag_order)


def prop(aci_class: str, wire: str) -> Prop:
    """The semantics of ``aci_class.wire``.

    Args:
        aci_class: The wire class name.
        wire: The wire property name (``"dn"`` included).

    Returns:
        The memoised :class:`Prop`.

    Raises:
        Refusal: The class is unknown, or carries no such property.
    """
    return _prop(aci_class, wire)


@cache
def _prop(aci_class: str, wire: str) -> Prop:
    check_class(aci_class)
    kinds = catalog.class_meta(aci_class).wire_to_kind
    if wire not in kinds:
        raise Refusal(f"class {aci_class!r} has no property {wire!r}")
    doc = catalog.prop_meta(aci_class, wire)
    default = "" if doc.default is None else to_wire(doc.default)
    field = _model_field(aci_class, wire)
    kind = kinds[wire]
    aliases = _named_aliases(field) if field is not None else None
    names = tuple(value for value in doc.enum_values if value != "defaultValue")
    return Prop(
        aci_class=aci_class,
        wire=wire,
        kind=kind,
        default=default,
        aliases=aliases,
        flag_order=names,
        field=field,
        # Only a class without a generated model lacks the table outright; a
        # generated class's operational number (pcTag) keeps reading as a number.
        unresolved=(
            kind in ("int", "float")
            and aliases is None
            and bool(names)
            and model_class(aci_class) is None
        ),
    )


def _model_field(aci_class: str, wire: str) -> FieldInfo | None:
    """The generated model field carrying *wire*, if the class has a model."""
    model = model_class(aci_class)
    if model is None:
        return None
    for name, info in model.model_fields.items():
        if (info.validation_alias or name) == wire:
            return info
    return None


def _named_aliases(info: FieldInfo) -> Mapping[str, str] | None:
    """The number → name table of a named-number field, read from its validators.

    A giant register rides in the field metadata as a :class:`NamedRegister`;
    an inlined table is the closure of the field's ``named_number`` validator.
    """
    for meta in info.metadata:
        if isinstance(meta, NamedRegister):
            return meta.aliases
    for meta in info.metadata:
        func: object = getattr(meta, "func", None)
        if not callable(func) or not getattr(func, "__qualname__", "").startswith("named_number."):
            continue
        table = inspect.getclosurevars(func).nonlocals.get("aliases")
        if isinstance(table, Mapping):
            entries = cast("Mapping[object, object]", table)
            return {str(number): str(name) for number, name in entries.items()}
    return None


def _parse_number(value: str) -> float | None:
    """A decimal or ``0x`` hexadecimal string as a number — ``None`` otherwise."""
    text = value.strip()
    if not text:
        return None
    try:
        number = float(int(text, 16)) if text.lower().startswith(("0x", "-0x")) else float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None
