"""The fault-code register — pinned before it is shared across five models.

Five generated models name their objects with a fault code: ``faultInst.code``,
``callhomeSmartCallhomeFault.code`` and the ``condition_code`` of
``faultLcP``, ``faultSevAsnP`` and ``eventSevAsnP``.  Each of them declares the
very same named number: 80,512 names (``F…``, ``E…`` and ``generic``) and an
alias table of 80,512 entries (``"1000"`` → ``"F1000"``, ``"0"`` →
``"generic"``).  Inlined twice per model (a ``Literal`` and the
``named_number`` dict), that is ~5.15 MB of source per file and ~0.5 s to
import one of them.

The register is emitted **once**, in ``niwaki.models._generated._registers``,
and the five fields are typed ``int | str`` with validators instead of
``int | Literal[<80,512 names>]`` (accepted static-type change).  This module
is the equivalence net around that move:

* the behaviour pins (``TestBehaviour``) were measured on the inlined models
  and must stay green before **and** after the register is shared — every
  sensitive case (``"1000"``, ``True``, ``1000.0``, bytes, whitespace, …) is
  pinned exactly as pydantic resolved it against ``int | Literal[...]``;
* the content pins freeze the register itself (sha256 of its sorted names and
  of its sorted alias pairs, computed from the inlined annotations);
* the sharing and size guards fail until the register is shared.

Register access contract (what the implementation must provide)
---------------------------------------------------------------
* ``niwaki.models._wire.NamedRegister`` — an immutable value with two
  attributes: ``names`` (a ``frozenset[str]`` of every name the field accepts)
  and ``aliases`` (a ``Mapping[str, str]``: the number as a decimal string →
  its name, the same shape :func:`niwaki.models._wire.named_number` takes).
* A register-backed field carries its ``NamedRegister`` **instance** directly
  in its ``Annotated[...]`` metadata, so it is exactly one element of
  ``FieldInfo.metadata``.
* That instance is a module-level attribute of
  ``niwaki.models._generated._registers``, and the five fields reference the
  **same** object (``is``), not five equal copies.
* ``FieldInfo.annotation`` of the five fields is exactly ``int | str``.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

from niwaki.models.base import ManagedObject

if TYPE_CHECKING:
    from niwaki.models._wire import NamedRegister

# ── The five register-backed fields ───────────────────────────────────────────

# (generated package, class, python field, RN prefix).  The wire name of the
# five fields is ``code``.
_FIELDS: list[tuple[str, str, str, str]] = [
    ("fault", "faultInst", "code", "fault"),
    ("fault", "faultLcP", "condition_code", "flcp"),
    ("fault", "faultSevAsnP", "condition_code", "fsevp"),
    ("event", "eventSevAsnP", "condition_code", "esevp"),
    ("callhome", "callhomeSmartCallhomeFault", "code", "code"),
]

_WIRE = "code"

FIELDS = [pytest.param(pkg, cls, field, rn, id=cls) for pkg, cls, field, rn in _FIELDS]

# ── The register, frozen (measured on the inlined models, 2026-10-26) ─────────

#: Number of names (71,561 ``F…``, 8,950 ``E…``, ``generic``) — and of aliases.
REGISTER_SIZE = 80_512

#: sha256 of ``"\n".join(sorted(names))`` (see :func:`_names_digest`).
NAMES_SHA256 = "43929fb82a1cfb1ea91037e84e0ec6c5439f2a03d2912aec0adebcc419032725"

#: sha256 of ``"\n".join(f"{k}\t{v}" for k, v in sorted(aliases.items()))``.
ALIASES_SHA256 = "78f2f6b462b557a1ff8ca493acd02d9ea37d7b1238e16715e5bedcd6d18824e5"

#: The first and last names of the inlined ``Literal`` (declaration order),
#: read with ``typing.get_args`` before the register was shared.
FIRST_NAME = "E4194310"
LAST_NAME = "generic"
LAST_F_NAME = "F999982"

#: The generated tree, and the size above which a literal is a register.
_GENERATED = Path(__file__).resolve().parents[2] / "src" / "niwaki" / "models" / "_generated"
_THRESHOLD = 1_000


# ── Helpers ───────────────────────────────────────────────────────────────────


@cache
def _model(pkg: str, cls: str) -> type[ManagedObject]:
    """Import one generated model (cached: the inlined ones cost ~0.5 s each)."""
    module = importlib.import_module(f"niwaki.models._generated.{pkg}.{cls}")
    model: type[ManagedObject] = getattr(module, cls)
    return model


def _build(pkg: str, cls: str, field: str, value: object) -> ManagedObject:
    """Construct the model with only its register-backed field set."""
    kwargs: dict[str, Any] = {field: value}
    return _model(pkg, cls)(**kwargs)


def _value(obj: ManagedObject, field: str) -> object:
    value: object = getattr(obj, field)
    return value


def _names_digest(names: frozenset[str] | set[str]) -> str:
    return hashlib.sha256("\n".join(sorted(names)).encode()).hexdigest()


def _aliases_digest(aliases: Mapping[str, str]) -> str:
    pairs = "\n".join(f"{key}\t{name}" for key, name in sorted(aliases.items()))
    return hashlib.sha256(pairs.encode()).hexdigest()


def _registers_module() -> ModuleType:
    return importlib.import_module("niwaki.models._generated._registers")


def _shared_register(pkg: str, cls: str, field: str) -> NamedRegister:
    """The ``NamedRegister`` a field carries in its metadata (see the contract)."""
    from niwaki.models._wire import NamedRegister

    info = _model(pkg, cls).model_fields[field]
    found = [item for item in info.metadata if isinstance(item, NamedRegister)]
    assert len(found) == 1, (
        f"{cls}.{field} must carry exactly one NamedRegister in its Annotated "
        f"metadata, found {len(found)}: {[type(m).__name__ for m in info.metadata]}"
    )
    return found[0]


# ── Behaviour: pinned on the inlined models, must not move ────────────────────


@pytest.mark.parametrize(("pkg", "cls", "field", "rn"), FIELDS)
class TestBehaviour:
    """What the five fields accept, store and reject — exactly as measured."""

    @pytest.mark.parametrize("name", [FIRST_NAME, LAST_F_NAME, LAST_NAME, "F1000"])
    def test_a_declared_name_is_stored_verbatim(
        self, pkg: str, cls: str, field: str, rn: str, name: str
    ) -> None:
        value = _value(_build(pkg, cls, field, name), field)
        assert value == name
        assert type(value) is str

    @pytest.mark.parametrize(
        ("number", "name"),
        [(1000, "F1000"), (0, "generic"), (4194310, "E4194310"), (999982, "F999982")],
    )
    def test_a_number_with_a_name_is_stored_under_the_name(
        self, pkg: str, cls: str, field: str, rn: str, number: int, name: str
    ) -> None:
        value = _value(_build(pkg, cls, field, number), field)
        assert value == name
        assert type(value) is str

    @pytest.mark.parametrize("number", [1, 999999999, -1, 2**63])
    def test_a_number_without_a_name_stays_a_number(
        self, pkg: str, cls: str, field: str, rn: str, number: int
    ) -> None:
        # No ge/le bounds on these fields: negatives and >64-bit pass too.
        value = _value(_build(pkg, cls, field, number), field)
        assert value == number
        assert type(value) is int

    @pytest.mark.parametrize(
        ("given", "stored"),
        [
            # A numeric string goes through int's lax parsing, then the alias.
            ("1000", "F1000"),
            ("0", "generic"),
            (" 1000", "F1000"),
            ("+1000", "F1000"),
            ("1_000", "F1000"),
            ("1000.0", "F1000"),
            ("999999999", 999999999),
            ("-1", -1),
            # bool is an int in lax mode: True has no name, False is 0.
            (True, 1),
            (False, "generic"),
            # An integral float is the int.
            (1000.0, "F1000"),
        ],
    )
    def test_sensitive_inputs_resolve_as_measured(
        self, pkg: str, cls: str, field: str, rn: str, given: object, stored: object
    ) -> None:
        value = _value(_build(pkg, cls, field, given), field)
        assert value == stored
        assert type(value) is type(stored)

    @pytest.mark.parametrize(
        "given",
        [
            "bogus",
            "F1",  # well-formed, absent from the register
            "E1",  # well-formed, absent from the register
            "f1000",  # names are case-sensitive
            "F1000 ",
            " F1000",
            "",
            "0x3e8",
            "1e3",
            b"F1000",
            bytearray(b"F1000"),
            None,
            1000.5,
            1000.0000001,
            float("nan"),
            1e20,
            [1000],
        ],
        ids=repr,
    )
    def test_anything_else_is_refused(
        self, pkg: str, cls: str, field: str, rn: str, given: object
    ) -> None:
        with pytest.raises(ValidationError):
            _build(pkg, cls, field, given)

    def test_the_default_is_generic(self, pkg: str, cls: str, field: str, rn: str) -> None:
        obj = _model(pkg, cls)()
        assert _value(obj, field) == "generic"
        assert obj.rn == f"{rn}-generic"
        assert obj.to_apic() == {cls: {"attributes": {_WIRE: "generic"}}}

    @pytest.mark.parametrize(
        ("given", "suffix"),
        [
            ("F1000", "F1000"),
            (1000, "F1000"),
            ("1000", "F1000"),
            (0, "generic"),
            ("E4194310", "E4194310"),
            (999999999, "999999999"),
            (-1, "-1"),
        ],
    )
    def test_rn_uses_the_stored_value(
        self, pkg: str, cls: str, field: str, rn: str, given: object, suffix: str
    ) -> None:
        assert _build(pkg, cls, field, given).rn == f"{rn}-{suffix}"

    @pytest.mark.parametrize(
        ("given", "wire"),
        [(1000, "F1000"), ("F1000", "F1000"), (0, "generic"), (999999999, "999999999")],
    )
    def test_to_apic_sends_the_stored_value(
        self, pkg: str, cls: str, field: str, rn: str, given: object, wire: str
    ) -> None:
        assert _build(pkg, cls, field, given).to_apic() == {cls: {"attributes": {_WIRE: wire}}}

    @pytest.mark.parametrize(
        ("wire", "read"),
        [
            ("F1000", "F1000"),
            ("generic", "generic"),
            ("E4194310", "E4194310"),
            # The read path runs no validator: a number is coerced to int and
            # NOT canonicalised to its name; an unknown name is kept as-is.
            ("1000", 1000),
            ("0", 0),
            ("999999999", 999999999),
            ("bogus", "bogus"),
        ],
    )
    def test_from_apic_reads_tolerantly(
        self, pkg: str, cls: str, field: str, rn: str, wire: str, read: object
    ) -> None:
        obj = ManagedObject.from_apic({cls: {"attributes": {_WIRE: wire}}})
        assert type(obj) is _model(pkg, cls)
        value = _value(obj, field)
        assert value == read
        assert type(value) is type(read)
        assert obj.rn == f"{rn}-{wire}"
        assert obj.to_apic() == {cls: {"attributes": {_WIRE: wire}}}

    @pytest.mark.parametrize("given", ["F1000", 1000, "generic", 999999999])
    def test_to_apic_from_apic_round_trip(
        self, pkg: str, cls: str, field: str, rn: str, given: object
    ) -> None:
        built = _build(pkg, cls, field, given)
        back = ManagedObject.from_apic(built.to_apic())
        assert type(back) is type(built)
        assert _value(back, field) == _value(built, field)
        assert back.rn == built.rn
        assert back.to_apic() == built.to_apic()

    def test_assignment_is_not_validated(self, pkg: str, cls: str, field: str, rn: str) -> None:
        # Models do not validate on assignment: no canonicalisation, no refusal.
        obj = _build(pkg, cls, field, "F1000")
        setattr(obj, field, 1001)
        assert _value(obj, field) == 1001
        assert obj.rn == f"{rn}-1001"
        setattr(obj, field, "bogus")
        assert _value(obj, field) == "bogus"


# ── The register itself: frozen content, shared once ─────────────────────────


@pytest.mark.parametrize(("pkg", "cls", "field", "rn"), FIELDS)
def test_register_content_is_frozen(pkg: str, cls: str, field: str, rn: str) -> None:
    """The shared register holds exactly the 80,512 names and aliases inlined before."""
    register = _shared_register(pkg, cls, field)
    assert isinstance(register.names, frozenset)
    assert len(register.names) == REGISTER_SIZE
    assert len(register.aliases) == REGISTER_SIZE
    assert {FIRST_NAME, LAST_F_NAME, LAST_NAME} <= register.names
    assert _names_digest(register.names) == NAMES_SHA256
    assert _aliases_digest(register.aliases) == ALIASES_SHA256


def test_every_register_name_is_accepted_by_fault_inst() -> None:
    """All 80,512 names construct a ``faultInst`` verbatim (~0.2 s measured)."""
    register = _shared_register("fault", "faultInst", "code")
    refused = [
        name
        for name in sorted(register.names)
        if _value(_build("fault", "faultInst", "code", name), "code") != name
    ]
    assert not refused, f"{len(refused)} register names not stored verbatim: {refused[:5]}"


def test_every_register_alias_canonicalises_on_fault_inst() -> None:
    """All 80,512 numbers construct a ``faultInst`` stored under their name."""
    register = _shared_register("fault", "faultInst", "code")
    wrong = [
        (number, name)
        for number, name in sorted(register.aliases.items())
        if _value(_build("fault", "faultInst", "code", int(number)), "code") != name
    ]
    assert not wrong, f"{len(wrong)} aliases not canonicalised: {wrong[:5]}"


@pytest.mark.parametrize(("pkg", "cls", "field", "rn"), FIELDS[1:])
def test_a_sample_of_register_names_is_accepted(pkg: str, cls: str, field: str, rn: str) -> None:
    """The four other models accept a spread sample of the register, both ways."""
    register = _shared_register(pkg, cls, field)
    names = sorted(register.names)[::97]
    aliases = sorted(register.aliases.items())[::97]
    assert len(names) > 800
    for name in names:
        assert _value(_build(pkg, cls, field, name), field) == name
    for number, name in aliases:
        assert _value(_build(pkg, cls, field, int(number)), field) == name


def test_registers_are_shared() -> None:
    """The five fields reference one register object, defined in ``_registers``."""
    registers = [_shared_register(pkg, cls, field) for pkg, cls, field, _ in _FIELDS]
    first = registers[0]
    assert all(register is first for register in registers), (
        "the five fault-code fields must share ONE register object, not equal copies"
    )
    module = _registers_module()
    assert any(value is first for value in vars(module).values()), (
        "the shared register must be a module-level attribute of "
        "niwaki.models._generated._registers"
    )
    for pkg, cls, field, _ in _FIELDS:
        annotation: Any = _model(pkg, cls).model_fields[field].annotation
        assert annotation == int | str, f"{cls}.{field} is typed {annotation!r}"


# ── No generated module inlines a giant literal ───────────────────────────────

#: Generated modules allowed to hold a container literal over the threshold:
#: lookup tables that are not named-number registers.
_KNOWN_TABLES: dict[str, str] = {
    "__init__.py": "_PKG_MAP — one entry per generated class, the lazy-import index",
    "enums/MonTargetScope2.py": "a StrEnum's alias table (monitoring target classes)",
}


def _giant_literals(path: Path) -> list[tuple[int, str, int]]:
    """Every container literal in *path* over the threshold: ``(line, kind, size)``."""
    found: list[tuple[int, str, int]] = []
    for node in ast.walk(ast.parse(path.read_bytes(), filename=str(path))):
        match node:
            case ast.Subscript(value=ast.Name(id="Literal") | ast.Attribute(attr="Literal")):
                elements = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
                kind, size = "Literal", len(elements)
            case ast.Dict(keys=keys):
                kind, size = "dict", len(keys)
            case ast.Set(elts=elts) | ast.List(elts=elts) | ast.Tuple(elts=elts):
                kind, size = type(node).__name__.lower(), len(elts)
            case _:
                continue
        if size > _THRESHOLD:
            found.append((node.lineno, kind, size))
    return found


def test_no_generated_module_inlines_a_giant_literal() -> None:
    """No ``Literal``, dict, set, list or tuple literal over 1,000 elements.

    A named number past the threshold lives in the shared register, encoded
    compactly — not as an 80,512-element literal parsed on every import.
    """
    offenders: dict[str, list[tuple[int, str, int]]] = {}
    for path in sorted(_GENERATED.rglob("*.py")):
        rel = path.relative_to(_GENERATED).as_posix()
        if rel in _KNOWN_TABLES:
            continue
        if found := _giant_literals(path):
            offenders[rel] = found
    assert not offenders, (
        f"generated modules inlining a literal over {_THRESHOLD:,} elements "
        f"(line, kind, size): {offenders}"
    )


def test_known_tables_are_still_giant() -> None:
    """An allowlisted table that shrank under the threshold is stale: remove it."""
    for rel in _KNOWN_TABLES:
        path = _GENERATED / rel
        assert path.is_file(), f"stale _KNOWN_TABLES entry (file gone): {rel}"
        assert _giant_literals(path), f"stale _KNOWN_TABLES entry (under threshold): {rel}"
