"""Field-level documentation data — the source of the generated attribute tables.

Everything the DSL reference needs to describe **one keyword argument**: its
Python name, the APIC wire alias, its type, the allowed values of an enum, the
default, and Cisco's own definition (piped into the models by the codegen).

The data is introspected from the **generated models** (the same objects the
runtime validates against), so a table can never disagree with what the SDK
actually accepts.  The one exception is the per-value meaning of an enum: those
live as *attribute docstrings* on the generated ``StrEnum`` members, which
Python discards at runtime — they are read back from the source with ``ast``.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from pathlib import Path
from types import UnionType
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic.fields import FieldInfo

from niwaki.models.base import ManagedObject

# Housekeeping fields every ACI class carries — noise in an attribute table.
_HOUSEKEEPING = frozenset({"children", "annotation", "userdom", "display_name"})

Kind = Literal["naming", "sugar", "attr"]


@dataclass(frozen=True)
class FieldDoc:
    """One documented keyword argument of a maker (or ``set()``) call.

    Attributes:
        name: Python keyword (``arp_flooding``).
        wire: APIC attribute name (``arpFlood``); equal to *name* when the
            schema name is already readable.
        kind: ``naming`` (positional, forms the RN), ``sugar`` (curated
            shorthand) or ``attr`` (plain attribute).
        type_str: Rendered type (``str``, ``bool``, ``int``, or the enum name).
        enum: Name of the ``StrEnum`` class when the field is an enum.
        values: Allowed values of the enum (empty otherwise).
        default: Rendered default, or ``None`` when there is none.
        description: Cisco's definition of the field (may be empty).
    """

    name: str
    wire: str
    kind: Kind
    type_str: str
    enum: str | None
    values: tuple[str, ...]
    default: str | None
    description: str


def _unwrap(annotation: Any) -> Any:
    """Strip ``Annotated[...]`` down to the underlying type."""
    if get_origin(annotation) is Annotated:
        return get_args(annotation)[0]
    return annotation


def _scalar_type(annotation: Any) -> str:
    """Render a non-enum, non-flags field's type: int, float, or str.

    A number the APIC may *name* arrives as ``int | Literal["unspecified", …]``
    (the engineer writes ``80``, the APIC stores ``"http"``); it is still a
    number, and documenting it as ``str`` would hide that.  A plain ``int`` or
    ``float`` renders as itself; everything else is a validated string.
    """
    if annotation is int:
        return "int"
    if annotation is float:
        return "float"
    if get_origin(annotation) in (Union, UnionType):
        members = {
            get_args(arg)[0] if get_origin(arg) is Annotated else arg
            for arg in get_args(annotation)
        }
        if int in members:
            return "int"
        if float in members:
            return "float"
    return "str"


def _render_type(annotation: Any) -> tuple[str, str | None, tuple[str, ...]]:
    """Resolve a field annotation to ``(type_str, enum_name, values)``.

    The single place the reference decides how a type reads, so a naming prop
    and a plain attribute of the same shape document identically — a naming
    port (``first_source_port``) is a number, not a string, and must say so.
    """
    unwrapped = _unwrap(annotation)
    if isinstance(unwrapped, type) and issubclass(unwrapped, StrEnum):
        return unwrapped.__name__, unwrapped.__name__, tuple(m.value for m in unwrapped)
    member = _flags_member(unwrapped)
    if member is not None:
        return f"set of {member.__name__}", member.__name__, tuple(m.value for m in member)
    if unwrapped is bool:
        return "bool", None, ()
    return _scalar_type(unwrapped), None, ()


def _flags_member(annotation: Any) -> type[StrEnum] | None:
    """The member enum of a ``Flags[E]`` bitmask, or ``None`` when it is not one.

    A bitmask field is ``Flags[E]`` (a PEP 695 alias resolving to
    ``frozenset[E] | set[E] | str``).  ``get_origin`` on it returns the alias,
    so its member enum is read from the args — this is what lets the reference
    document a set of flags with the same allowed-values table as an enum.
    """
    from niwaki.models._wire import Flags

    if get_origin(annotation) is Flags:
        (member,) = get_args(annotation)
        if isinstance(member, type) and issubclass(member, StrEnum):
            return member
    return None


def _render_default(info: FieldInfo) -> str | None:
    """Render a field's default for the table, or ``None`` when there is none.

    A bitmask carries its default through ``default_factory`` (a frozenset), so
    ``info.default`` is ``PydanticUndefined`` — printing that verbatim is what
    put the string "PydanticUndefined" in the reference.  The factory is called
    to recover the real default, and a set of flags renders as its members.
    """
    from pydantic_core import PydanticUndefined

    default = info.default
    if default is PydanticUndefined:
        if info.default_factory is None:
            return None
        default = info.default_factory()  # type: ignore[call-arg]
    if isinstance(default, frozenset | set):
        members = sorted(m.value if isinstance(m, StrEnum) else str(m) for m in default)
        return ", ".join(members) or None
    if isinstance(default, StrEnum):
        default = default.value
    return None if default in ("", None) else str(default)


def _wire_name(info: FieldInfo) -> str | None:
    """The APIC attribute name of a field, or ``None`` when it is its own name.

    The wire name lives in ``serialization_alias``, not ``alias`` — see
    :meth:`~niwaki.models.base.ManagedObject._get_alias_map` for why the plain
    ``alias`` is unusable (a type checker reads it and then rejects the readable
    Python name it was renamed to).
    """
    return info.serialization_alias


def field_docs(cls: type[ManagedObject], sugar: dict[str, str]) -> list[FieldDoc]:
    """Describe every keyword a maker for *cls* accepts, naming props first.

    Args:
        cls: Generated model class of the position.
        sugar: Curated sugar parameters of that class (``{name: annotation}``).

    Returns:
        Naming props (positional), then the curated sugar parameters, then the
        plain attributes — the order the generated signature uses.
    """
    naming = list(cls._naming_props)  # pyright: ignore[reportPrivateUsage]
    docs: list[FieldDoc] = []

    for name in naming:
        info = cls.model_fields.get(name)
        type_str, enum_name, values = (
            _render_type(info.annotation) if info is not None else ("str", None, ())
        )
        docs.append(
            FieldDoc(
                name=name,
                wire=(_wire_name(info) if info else name) or name,
                kind="naming",
                type_str=type_str,
                enum=enum_name,
                values=values,
                default=None,
                description=(info.description if info and info.description else ""),
            )
        )

    for name, annotation in sugar.items():
        docs.append(
            FieldDoc(
                name=name,
                wire="—",
                kind="sugar",
                type_str=annotation.replace(" | None", ""),
                enum=None,
                values=(),
                default=None,
                description="Curated shorthand — expanded to the real schema fields.",
            )
        )

    for name, info in cls.model_fields.items():
        if name in naming or name in _HOUSEKEEPING:
            continue
        # A bitmask (Flags[E]) documents its members like an enum; a named number
        # (int | Literal[...]) reads as a number, not str — see _render_type.
        type_str, enum_name, values = _render_type(info.annotation)
        default_str = _render_default(info)

        docs.append(
            FieldDoc(
                name=name,
                wire=_wire_name(info) or name,
                kind="attr",
                type_str=type_str,
                enum=enum_name,
                values=values,
                default=default_str,
                description=info.description or "",
            )
        )

    return docs


def class_definition(cls: type[ManagedObject]) -> str:
    """Cisco's definition of the class, from the generated docstring.

    The generated docstring is ``"ACI Managed Object: … — <label>.\\n\\n<Cisco
    definition>\\n\\nRN format: …"``; the definition is the second paragraph,
    when Cisco documented the class.
    """
    doc = inspect.getdoc(cls) or ""
    paragraphs = [p.strip() for p in doc.split("\n\n") if p.strip()]
    if len(paragraphs) < 2:
        return ""
    second = paragraphs[1]
    if second.startswith("RN format") or second.startswith("The APIC can flag"):
        return ""
    return " ".join(second.split())


@cache
def enum_members(enum_cls: type[StrEnum]) -> tuple[tuple[str, str], ...]:
    """Return ``((value, meaning), …)`` for a generated enum.

    Cisco's per-value meanings are emitted as **attribute docstrings** on the
    generated members — a static convention Python drops at runtime, so the
    source is parsed instead.  Values without a documented meaning come back
    with an empty string.
    """
    meanings: dict[str, str] = {}
    try:
        source = Path(inspect.getfile(enum_cls)).read_text()
    except (OSError, TypeError):  # pragma: no cover — generated files are on disk
        source = ""

    if source:
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or node.name != enum_cls.__name__:
                continue
            body = node.body
            for index, statement in enumerate(body):
                if not isinstance(statement, ast.Assign):
                    continue
                if not isinstance(statement.value, ast.Constant):
                    continue
                value = statement.value.value
                if not isinstance(value, str):
                    continue
                nxt = body[index + 1] if index + 1 < len(body) else None
                if (
                    isinstance(nxt, ast.Expr)
                    and isinstance(nxt.value, ast.Constant)
                    and isinstance(nxt.value.value, str)
                ):
                    meanings[value] = " ".join(nxt.value.value.split())

    return tuple((member.value, meanings.get(member.value, "")) for member in enum_cls)


def enum_anchor(enum_name: str) -> str:
    """Explicit MyST target of an enum section on the enums page."""
    return f"enum-{enum_name.lower()}"


def position_anchor(key: str) -> str:
    """Explicit MyST target of a position page."""
    return f"vocab-{key.replace('.', '-')}"


def position_slug(key: str) -> str:
    """File name (without extension) of a position page inside its domain."""
    return key.replace(".", "-")
