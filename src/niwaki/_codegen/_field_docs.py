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
from typing import Annotated, Any, Literal, get_args, get_origin

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
        docs.append(
            FieldDoc(
                name=name,
                wire=(info.alias if info and info.alias else name),
                kind="naming",
                type_str="str",
                enum=None,
                values=(),
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
        annotation = _unwrap(info.annotation)
        enum_name: str | None = None
        values: tuple[str, ...] = ()
        if isinstance(annotation, type) and issubclass(annotation, StrEnum):
            enum_name = annotation.__name__
            values = tuple(member.value for member in annotation)
            type_str = enum_name
        elif annotation is bool:
            type_str = "bool"
        elif annotation is int:
            type_str = "int"
        else:
            type_str = "str"

        default = info.default
        if isinstance(default, StrEnum):
            default = default.value
        default_str = None if default in ("", None) else str(default)

        docs.append(
            FieldDoc(
                name=name,
                wire=(info.alias or name),
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
