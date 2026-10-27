"""The generator's seam for giant named numbers — shared registers.

A named number whose names exceed ``NAMED_REGISTER_THRESHOLD`` (1,000) is not
inlined as ``int | Literal[...]`` plus a ``named_number({...})`` dict: its
content goes into a shared register, emitted once in
``models/_generated/_registers.py``, and the field line only references it.
Below or at the threshold, nothing changes — byte for byte.

Seam contract (what ``niwaki._codegen.generate`` must provide)
--------------------------------------------------------------
* ``NAMED_REGISTER_THRESHOLD: int`` — ``1_000``.  A named number is
  register-backed when ``len(prop["values"]) > NAMED_REGISTER_THRESHOLD``.
* ``RegisterTable`` — collects the registers of one generation run:

  - ``RegisterTable()`` starts empty; ``len(table)`` is the number of distinct
    registers collected.
  - ``table.add(names, aliases) -> str`` records a register and returns its
    symbol: a Python identifier, module-level in the rendered ``_registers``
    module.  Content identity is ``(frozenset(names), dict(aliases))``: adding
    the same content again — in any order — returns the same symbol and adds
    nothing (one register shared by every field that declares it).  It raises
    ``ValueError`` on an empty register, on a duplicated name, and on an alias
    whose target is not one of the names.
  - ``table.render() -> str`` is the full source of ``_registers.py``:
    deterministic, defining each symbol as a
    :class:`niwaki.models._wire.NamedRegister` (``.names`` /
    ``.aliases``), and inlining no container literal over the threshold.

* ``_numeric_field_line(py_name, prop, alias_arg, secure, default, desc_arg,
  *, registers: RegisterTable | None = None) -> str``:

  - at or under the threshold: today's line, byte-identical, whether or not a
    table is given, and the table is left untouched;
  - over the threshold: the names and aliases go to ``registers.add`` and the
    line references the returned symbol instead of inlining them; the static
    type is ``int | str``; alias, description and default are kept.  Without a
    table it raises ``ValueError`` (a giant register is never inlined
    silently).
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING, Any

import pytest

from niwaki._codegen.generate import _numeric_field_line

if TYPE_CHECKING:
    from niwaki._codegen.generate import RegisterTable

_ALIAS = "validation_alias='code', serialization_alias='code'"
_DESC = "description='A fault code.'"


def _table() -> RegisterTable:
    from niwaki._codegen.generate import RegisterTable

    return RegisterTable()


def _threshold() -> int:
    from niwaki._codegen.generate import NAMED_REGISTER_THRESHOLD

    return NAMED_REGISTER_THRESHOLD


def _register(size: int) -> tuple[list[str], dict[str, str]]:
    """A synthetic fault register: ``generic`` plus ``F1``…, aliased by number."""
    names = ["generic"] + [f"F{n}" for n in range(1, size)]
    aliases = {"0": "generic"} | {str(n): f"F{n}" for n in range(1, size)}
    return names, aliases


def _prop(names: list[str], aliases: dict[str, str]) -> dict[str, Any]:
    return {"python_type": "int", "values": names, "aliases": aliases}


def _giant_containers(source: str, threshold: int) -> list[tuple[int, str, int]]:
    found: list[tuple[int, str, int]] = []
    for node in ast.walk(ast.parse(source)):
        match node:
            case ast.Dict(keys=keys):
                size = len(keys)
            case ast.Set(elts=elts) | ast.List(elts=elts) | ast.Tuple(elts=elts):
                size = len(elts)
            case _:
                continue
        if size > threshold:
            found.append((node.lineno, type(node).__name__, size))
    return found


# ── Today's lines, pinned byte for byte ───────────────────────────────────────

_PORT_PROP: dict[str, Any] = {
    "python_type": "int",
    "values": ["http", "https", "unspecified"],
    "aliases": {"80": "http", "443": "https", "0": "unspecified"},
    "ge": 0,
    "le": 65535,
}
_PORT_ALIAS = "validation_alias='dFromPort', serialization_alias='dFromPort'"
_PORT_DESC = "description='From port.'"
_PORT_LINE = (
    "first_destination_port: Annotated[Annotated[int, Field(ge=0, le=65535)]"
    " | Literal['http', 'https', 'unspecified'],"
    " AfterValidator(named_number({'0': 'unspecified', '443': 'https', '80': 'http'})),"
    " Field(validation_alias='dFromPort', serialization_alias='dFromPort',"
    " description='From port.')] = 'unspecified'"
)
_GENERIC_PROP: dict[str, Any] = {
    "python_type": "int",
    "values": ["generic"],
    "aliases": {"0": "generic"},
}
_GENERIC_LINE = (
    "code: Annotated[int | Literal['generic'], AfterValidator(named_number({'0': 'generic'}))]"
    " = 'generic'"
)


def test_small_named_number_line_is_pinned() -> None:
    """The inlined form of a small named number (a port), as emitted today."""
    line = _numeric_field_line(
        "first_destination_port", _PORT_PROP, _PORT_ALIAS, False, "unspecified", _PORT_DESC
    )
    assert line == _PORT_LINE
    assert _numeric_field_line("code", _GENERIC_PROP, None, False, "generic", None) == (
        _GENERIC_LINE
    )


def test_threshold_is_one_thousand() -> None:
    assert _threshold() == 1_000


def test_small_named_number_ignores_the_table() -> None:
    """Under the threshold a table changes nothing, and collects nothing."""
    table = _table()
    line = _numeric_field_line(
        "first_destination_port",
        _PORT_PROP,
        _PORT_ALIAS,
        False,
        "unspecified",
        _PORT_DESC,
        registers=table,
    )
    assert line == _PORT_LINE
    line = _numeric_field_line("code", _GENERIC_PROP, None, False, "generic", None, registers=table)
    assert line == _GENERIC_LINE
    assert len(table) == 0


def test_exactly_at_threshold_stays_inline() -> None:
    """1,000 names is not *over* 1,000: inlined, identical with or without a table."""
    names, aliases = _register(_threshold())
    assert len(names) == _threshold()
    table = _table()
    with_table = _numeric_field_line(
        "code", _prop(names, aliases), _ALIAS, False, "generic", _DESC, registers=table
    )
    without = _numeric_field_line("code", _prop(names, aliases), _ALIAS, False, "generic", _DESC)
    assert with_table == without
    assert "Literal['generic', 'F1', " in with_table
    assert "'999': 'F999'" in with_table
    assert len(table) == 0


def test_over_threshold_references_a_shared_register() -> None:
    names, aliases = _register(_threshold() + 1)
    table = _table()
    line = _numeric_field_line(
        "condition_code", _prop(names, aliases), _ALIAS, False, "generic", _DESC, registers=table
    )
    assert len(table) == 1
    symbol = table.add(names, aliases)  # same content: same symbol, nothing added
    assert len(table) == 1
    assert symbol.isidentifier()
    assert symbol in line
    assert "Literal[" not in line
    assert "'F1'" not in line
    assert "'1000': 'F1000'" not in line
    assert line.startswith("condition_code: Annotated[int | str, ")
    assert _ALIAS in line
    assert _DESC in line
    assert line.endswith("= 'generic'")
    ast.parse(line)  # a well-formed annotated assignment


def test_over_threshold_without_a_table_raises() -> None:
    names, aliases = _register(_threshold() + 1)
    with pytest.raises(ValueError):
        _numeric_field_line("code", _prop(names, aliases), None, False, "generic", None)


def test_over_threshold_with_a_duplicate_name_raises() -> None:
    names, aliases = _register(_threshold() + 1)
    names.append("F1")
    with pytest.raises(ValueError):
        _numeric_field_line(
            "code", _prop(names, aliases), None, False, "generic", None, registers=_table()
        )


# ── The register table ────────────────────────────────────────────────────────


def test_identical_content_is_one_register() -> None:
    """Five fields declaring the same register share one symbol, in any order."""
    names, aliases = _register(2_000)
    table = _table()
    first = table.add(names, aliases)
    again = table.add(list(reversed(names)), dict(reversed(list(aliases.items()))))
    assert again == first
    assert len(table) == 1


def test_different_content_is_another_register() -> None:
    names, aliases = _register(2_000)
    table = _table()
    first = table.add(names, aliases)
    other = table.add([*names, "E1"], aliases)
    assert other != first
    assert len(table) == 2


@pytest.mark.parametrize(
    ("names", "aliases"),
    [
        pytest.param([], {}, id="empty"),
        pytest.param(["generic", "F1", "F1"], {"0": "generic"}, id="duplicate-name"),
        pytest.param(["generic", "F1"], {"0": "generic", "2": "F2"}, id="alias-to-unknown-name"),
    ],
)
def test_add_rejects_a_malformed_register(names: list[str], aliases: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        _table().add(names, aliases)


def test_rendered_module_defines_each_register() -> None:
    """Executing the rendered ``_registers`` source yields the registers, intact."""
    from niwaki.models._wire import NamedRegister

    big_names, big_aliases = _register(5_000)
    other_names, other_aliases = _register(1_500)
    table = _table()
    big = table.add(big_names, big_aliases)
    other = table.add(other_names, other_aliases)

    namespace: dict[str, Any] = {}
    exec(compile(table.render(), "_registers.py", "exec"), namespace)

    for symbol, names, aliases in (
        (big, big_names, big_aliases),
        (other, other_names, other_aliases),
    ):
        register = namespace[symbol]
        assert isinstance(register, NamedRegister)
        assert register.names == frozenset(names)
        assert dict(register.aliases) == aliases


def test_rendered_module_is_deterministic() -> None:
    names, aliases = _register(3_000)

    def render() -> str:
        table = _table()
        table.add(names, aliases)
        return table.render()

    assert render() == render()


def test_rendered_module_inlines_no_giant_literal() -> None:
    """The register is encoded compactly, not as an 80k-element literal."""
    names, aliases = _register(80_512)
    table = _table()
    table.add(names, aliases)
    assert _giant_containers(table.render(), _threshold()) == []
