"""Every ``Any`` in the hand-written SDK carries its reason — and there are few.

The project rule is "no lazy ``Any``": strict typing everywhere.  ``Any`` is
not a type, it is the absence of one — every value flowing through it escapes
both mypy and pyright, silently.  Raw APIC JSON has a real, recursive shape
(``niwaki.types``: ``JsonScalar`` / ``JsonValue`` / ``JsonObject``), so the
wire payloads do not need ``Any``; what remains is the handful of places where
``Any`` is genuinely the right answer (forwarded ``**kwargs``, a name-driven
``__getattr__``, the arity fallback of ``AsyncNiwaki.gather``, ...).

This guard makes the rule mechanical:

* **Justification.**  Every reference to ``Any`` in the hand-written source
  must sit on a physical line that carries a ``# Any: <reason>`` comment.  The
  comment may share the line with others (``# noqa: ...``,
  ``# type: ignore[...]``, ``# pyright: ignore[...]``).  An empty reason does
  not count.
* **No aliasing.**  An alias of ``Any`` — or of a type that contains it —
  is forbidden outright, justified or not: ``A = Any``,
  ``A: TypeAlias = Any``, ``type A = Any``, ``_Node = dict[str, Any]``,
  ``TypeAliasType("A", Any)``.  One justified alias used in twenty
  signatures would launder twenty escapes through one comment and one unit of
  budget.
* **Exact budget.**  The number of ``Any`` references equals
  :data:`_ANY_BUDGET` — exactly.  Above it, replace an ``Any`` rather than
  raising the number (raising it needs a reason in the commit that does it).
  Below it, the test fails too and says which number to write: the budget
  always records the current count, so a removed ``Any`` cannot quietly come
  back.

What counts as a reference (the scanner's contract, pinned by the self-tests
at the bottom of this module):

* ``Any`` and ``typing.Any`` (also ``typing_extensions.Any``, a module alias
  such as ``import typing as t`` → ``t.Any``, and a renamed import such as
  ``from typing import Any as Anything`` — wherever that import sits, even
  under ``if TYPE_CHECKING:``), wherever it appears in *code*: annotations of
  parameters (``*args`` / ``**kwargs`` included), returns, annotated
  assignments, ``type`` statements and type-parameter bounds, nested inside
  subscripts (``dict[str, Any]``).
* **String type expressions** are parsed and scanned: string annotations
  (``x: "Any"``), strings nested inside an annotation (``list["Any"]``), the
  value of an explicit ``TypeAlias`` (``X: TypeAlias = "dict[str, Any]"``),
  the type argument of ``cast("Any", x)`` / ``assert_type(x, "Any")`` /
  ``NewType("N", "Any")``, and the bound and constraints of
  ``TypeVar("T", bound="Any")``.  The value slots of ``Literal[...]`` and the
  metadata slots of ``Annotated[T, ...]`` are values, not types — not parsed.
* **Non-annotation uses count too** — ``cast(Any, x)``,
  ``TypeVar("T", bound=Any)``, ``Coroutine[Any, Any, T]`` in an expression.
  Deliberate choice: an ``Any`` in an expression is the same typing escape as
  one in an annotation (``cast(Any, x)`` is *the* canonical way to switch the
  type checker off for a value), so exempting it would just move the lazy
  ``Any`` one token to the left.
* Each reference is counted separately: ``Coroutine[Any, Any, T]`` is two.
* ``Any`` inside a docstring, a comment or an ordinary string literal is prose,
  not a type — never counted.  The ``from typing import Any`` statement itself
  is not a reference.

Out of scope, deliberately: dynamic spellings no reviewer would let through
and no static scanner can follow — ``getattr(typing, "Any")``,
``__import__("typing").Any``, ``from typing import *``, ``Any`` reached
through ``vars()`` / ``globals()``.  The guard catches laziness, not sabotage.

Scope: ``src/niwaki`` minus the generated trees (``models/_generated/``,
``design/_generated_cursors*``, ``domain/_child_map.py``) and the code
generator (``_codegen/``, excluded from the wheel).  Generated code is held to
its own standard by its generator and freshness guards.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src" / "niwaki"

# The exact count of ``Any`` references in scope (all of them justified).
# Above: replace an ``Any`` instead of raising this.  Below: lower it to the
# count the failure reports.
_ANY_BUDGET = 10

# ``# Any: <reason>`` anywhere inside the line's comment, possibly after other
# comments (a noqa or type-ignore, then ``# Any: ...``).  The reason must be
# non-empty.
_JUSTIFICATION = re.compile(r"#\s*Any:\s*\S")

_TYPING_MODULES = frozenset({"typing", "typing_extensions"})


@dataclass(frozen=True, order=True)
class AnyUse:
    """One reference to ``Any`` in a source file.

    Attributes:
        path: Display path of the file (relative to ``src/niwaki`` for the
            real scan, an arbitrary label for in-memory snippets).
        line: 1-based physical line of the reference.
        col: 0-based column of the reference (orders several uses per line).
        justified: Whether that physical line carries a ``# Any: <reason>``
            comment.
        in_alias: Whether the reference is part of a type-alias definition
            (forbidden whatever the comment says).
    """

    path: str
    line: int
    col: int
    justified: bool
    in_alias: bool = False

    def location(self) -> str:
        """Return the ``path:line`` form used in failure messages."""
        return f"{self.path}:{self.line}"


def _justified_lines(source: str) -> frozenset[int]:
    """Return the physical lines whose comment carries a ``# Any:`` reason.

    Comments are read with :mod:`tokenize`, so a ``# Any:`` that lives inside
    a string literal (a docstring quoting the convention, say) never justifies
    anything.
    """
    lines: set[int] = set()
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT and _JUSTIFICATION.search(token.string):
            lines.add(token.start[0])
    return frozenset(lines)


def _callee_name(func: ast.expr) -> str | None:
    """``cast`` for ``cast(...)`` and ``typing.cast(...)``; ``None`` otherwise."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


class _AnyCollector(ast.NodeVisitor):
    """Collect every code reference to ``Any`` in one module's AST.

    Imports are gathered first (:meth:`prime`), so a renamed ``Any`` is
    recognised wherever its import sits in the file.
    """

    def __init__(self, any_names: set[str], typing_aliases: set[str]) -> None:
        self.any_names = any_names
        self.typing_aliases = typing_aliases
        # (line, col, in_alias) of each reference, relative to the parsed source.
        self.hits: list[tuple[int, int, bool]] = []
        self._type_context = 0
        self._in_alias = False

    @classmethod
    def prime(cls, tree: ast.AST) -> _AnyCollector:
        """Build a collector that knows every ``Any`` spelling *tree* imports."""
        any_names = {"Any"}
        typing_aliases = set(_TYPING_MODULES)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in _TYPING_MODULES and alias.asname:
                        typing_aliases.add(alias.asname)
            elif isinstance(node, ast.ImportFrom) and node.module in _TYPING_MODULES:
                for alias in node.names:
                    if alias.name == "Any" and alias.asname:
                        any_names.add(alias.asname)
        return cls(any_names, typing_aliases)

    # ── helpers ──────────────────────────────────────────────────────────────

    def _hit(self, node: ast.expr) -> None:
        self.hits.append((node.lineno, node.col_offset, self._in_alias))

    def _is_typing_name(self, node: ast.expr, name: str) -> bool:
        """``name`` or ``typing.name`` (any typing module alias)."""
        if isinstance(node, ast.Name):
            return node.id == name
        return (
            isinstance(node, ast.Attribute)
            and node.attr == name
            and isinstance(node.value, ast.Name)
            and node.value.id in self.typing_aliases
        )

    def _visit_type(self, node: ast.expr | None) -> None:
        """Visit *node* as a type expression: its strings are types too."""
        if node is None:
            return
        self._type_context += 1
        try:
            self.visit(node)
        finally:
            self._type_context -= 1

    def _visit_value(self, node: ast.expr) -> None:
        """Visit *node* as a runtime value: its strings are prose."""
        saved, self._type_context = self._type_context, 0
        try:
            self.visit(node)
        finally:
            self._type_context = saved

    def _visit_alias_value(self, node: ast.expr, *, as_type: bool) -> None:
        saved, self._in_alias = self._in_alias, True
        try:
            if as_type:
                self._visit_type(node)
            else:
                self.visit(node)
        finally:
            self._in_alias = saved

    # ── references ───────────────────────────────────────────────────────────

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in self.any_names:
            self._hit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == "Any" and self._is_typing_name(node, "Any"):
            self._hit(node)
            return
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if not (self._type_context and isinstance(node.value, str)):
            return
        try:
            inner = ast.parse(node.value.strip(), mode="eval")
        except SyntaxError:
            return  # not a type expression — prose, not a reference
        sub = _AnyCollector(self.any_names, self.typing_aliases)
        sub._visit_type(inner.body)
        # A string is a single physical token; anchor its hits there.
        self.hits.extend((node.lineno, node.col_offset, self._in_alias) for _ in sub.hits)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if self._type_context and self._is_typing_name(node.value, "Literal"):
            self.visit(node.value)  # Literal["Any"] holds a value, not a type
            return
        if self._type_context and self._is_typing_name(node.value, "Annotated"):
            self.visit(node.value)
            elements = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
            self.visit(elements[0])
            for metadata in elements[1:]:
                self._visit_value(metadata)
            return
        self.generic_visit(node)

    # ── calls whose arguments are types ──────────────────────────────────────

    def visit_Call(self, node: ast.Call) -> None:
        self.visit(node.func)
        name = _callee_name(node.func)
        type_positions: set[int] = set()
        match name:
            case "cast":
                type_positions = {0}
            case "assert_type" | "NewType":
                type_positions = {1}
            case "TypeVar":
                type_positions = set(range(1, len(node.args)))
            case "TypeAliasType":
                type_positions = {1}
            case _:
                pass
        for index, arg in enumerate(node.args):
            if index in type_positions:
                if name == "TypeAliasType":
                    self._visit_alias_value(arg, as_type=True)
                else:
                    self._visit_type(arg)
            else:
                self._visit_value(arg)
        for keyword in node.keywords:
            if name == "TypeVar" and keyword.arg == "bound":
                self._visit_type(keyword.value)
            else:
                self._visit_value(keyword.value)

    # ── annotations ──────────────────────────────────────────────────────────

    def visit_arg(self, node: ast.arg) -> None:
        self._visit_type(node.annotation)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._visit_type(node.annotation)
        self.visit(node.target)
        if node.value is None:
            return
        if self._is_typing_name(node.annotation, "TypeAlias"):
            self._visit_alias_value(node.value, as_type=True)
        else:
            self._visit_value(node.value)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self.visit(target)
        value = node.value
        type_shaped = isinstance(value, ast.Name | ast.Attribute | ast.Subscript) or (
            isinstance(value, ast.BinOp) and isinstance(value.op, ast.BitOr)
        )
        if type_shaped:
            # ``A = Any`` / ``_Node = dict[str, Any]`` / ``A = Any | None``:
            # an old-style alias.
            self._visit_alias_value(value, as_type=False)
        else:
            self.visit(value)  # a computed value (call, comprehension, ...)

    def visit_TypeAlias(self, node: ast.TypeAlias) -> None:
        for type_param in node.type_params:
            self.visit(type_param)
        self._visit_alias_value(node.value, as_type=True)

    def visit_TypeVar(self, node: ast.TypeVar) -> None:
        self._visit_type(node.bound)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for type_param in node.type_params:
            self.visit(type_param)
        self.visit(node.args)
        self._visit_type(node.returns)
        for statement in node.body:
            self.visit(statement)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)


def scan_source(source: str, path: str) -> list[AnyUse]:
    """Return every ``Any`` reference in *source*, sorted by position.

    Args:
        source: Python source text of one module.
        path: Label reported in each :class:`AnyUse` (``path:line``).

    Returns:
        One :class:`AnyUse` per reference, with its justification status and
        whether it sits in a (forbidden) alias definition.

    Raises:
        SyntaxError: *source* does not parse.  Deliberately not swallowed — a
            file the guard cannot read must not pass as "no ``Any`` found".
    """
    tree = ast.parse(source, filename=path)
    collector = _AnyCollector.prime(tree)
    collector.visit(tree)
    justified = _justified_lines(source)
    return sorted(
        AnyUse(path=path, line=line, col=col, justified=line in justified, in_alias=in_alias)
        for line, col, in_alias in collector.hits
    )


def _in_scope(relative: Path) -> bool:
    """Whether a file under ``src/niwaki`` is hand-written (guarded) code."""
    parts = relative.parts
    if parts[0] == "_codegen":
        return False
    if parts[:2] == ("models", "_generated"):
        return False
    if parts[0] == "design" and len(parts) > 1 and parts[1].startswith("_generated_cursors"):
        return False
    return relative.as_posix() != "domain/_child_map.py"


def scoped_files() -> list[Path]:
    """Return the hand-written ``.py`` files of the SDK, sorted."""
    return sorted(path for path in _SRC.rglob("*.py") if _in_scope(path.relative_to(_SRC)))


def display_path(path: Path) -> str:
    """Return *path* relative to ``src/niwaki``, POSIX-style."""
    return path.relative_to(_SRC).as_posix()


def scan_tree() -> list[AnyUse]:
    """Scan the whole hand-written scope and return every ``Any`` reference."""
    uses: list[AnyUse] = []
    for path in scoped_files():
        uses.extend(scan_source(path.read_text(encoding="utf-8"), display_path(path)))
    return sorted(uses)


def _listing(uses: list[AnyUse]) -> tuple[int, str]:
    """``(distinct lines, "path:line (xN)" listing)`` of *uses* (already sorted)."""
    per_line = Counter(use.location() for use in uses)
    rows = [f"{loc} (x{count})" if count > 1 else loc for loc, count in per_line.items()]
    return len(per_line), "\n  ".join(rows)


# ── the guard on the real tree ───────────────────────────────────────────────


def test_scope_is_the_hand_written_sdk() -> None:
    """The scan sees the hand-written modules and none of the generated ones.

    A guard over an empty (or wrongly filtered) file set would pass
    vacuously; pin both sides of the filter.
    """
    files = {display_path(path) for path in scoped_files()}
    for expected in (
        "facade.py",
        "snapshot.py",
        "models/base.py",
        "models/_wire.py",
        "design/_cursor.py",
        "transport/session.py",
        "utils/response.py",
    ):
        assert expected in files, f"{expected} fell out of the Any guard's scope"
    leaked = sorted(
        f
        for f in files
        if f.startswith(("_codegen/", "models/_generated/", "design/_generated_cursors"))
        or f == "domain/_child_map.py"
    )
    assert not leaked, f"generated/codegen files leaked into the scope: {leaked}"


def test_every_any_is_justified() -> None:
    """Every ``Any`` in hand-written code carries a ``# Any: <reason>`` comment."""
    unjustified = [use for use in scan_tree() if not use.justified]
    lines, listing = _listing(unjustified)
    assert not unjustified, (
        f"{len(unjustified)} unjustified `Any` reference(s) on {lines} line(s) in "
        f"src/niwaki:\n  {listing}\n\n"
        "Hint: raw APIC JSON is `niwaki.types.JsonObject` / `JsonValue` (or "
        "`Mapping[str, object]` for a consumer); a value of unknown type is "
        "usually `object`.  If `Any` really is the right type, say why on the "
        "same line: `... # Any: <reason>`."
    )


def test_no_alias_launders_any() -> None:
    """No type alias names ``Any`` or a type containing it — justified or not."""
    aliased = [use for use in scan_tree() if use.in_alias]
    _, listing = _listing(aliased)
    assert not aliased, (
        f"`Any` aliased in src/niwaki:\n  {listing}\n\n"
        "An alias spreads one escape over every signature that uses it while "
        "costing one comment.  Spell `Any` at each use site, with its reason."
    )


def test_any_budget_is_exact() -> None:
    """The ``Any`` count equals :data:`_ANY_BUDGET` — no more, no less."""
    count = len(scan_tree())
    assert count <= _ANY_BUDGET, (
        f"{count} `Any` references in src/niwaki exceed the budget of {_ANY_BUDGET}.  "
        "Replace the new ones with a precise type (JsonValue, Mapping[str, object], "
        "object, a Protocol, a TypeVar) rather than raising the budget."
    )
    assert count >= _ANY_BUDGET, (
        f"only {count} `Any` references remain in src/niwaki: lower the budget to "
        f"{count} (_ANY_BUDGET in {Path(__file__).name}) so the removed ones "
        "cannot come back unnoticed."
    )


# ── the scanner's own contract ───────────────────────────────────────────────


def _scan(source: str) -> list[AnyUse]:
    return scan_source(source, "snippet.py")


def _lines(uses: list[AnyUse]) -> list[int]:
    return [use.line for use in uses]


def test_scanner_justified_reference_passes() -> None:
    uses = _scan(
        "from typing import Any\n"
        "def f(**kwargs: Any) -> None:  # Any: forwarded verbatim to httpx\n"
        "    pass\n"
    )
    assert len(uses) == 1
    assert uses[0].justified
    assert not uses[0].in_alias
    assert uses[0].location() == "snippet.py:2"


def test_scanner_unjustified_reference_fails() -> None:
    uses = _scan("from typing import Any\ndef f(x: Any) -> None:\n    pass\n")
    assert [(u.line, u.justified) for u in uses] == [(2, False)]


def test_scanner_import_statement_is_not_a_reference() -> None:
    assert _scan("from typing import Any\n") == []


def test_scanner_detects_typing_attribute_form() -> None:
    uses = _scan("import typing\nx: typing.Any = 1\n")
    assert _lines(uses) == [2]


def test_scanner_detects_aliased_typing_module() -> None:
    uses = _scan("import typing as t\ndef f() -> t.Any:\n    ...\n")
    assert _lines(uses) == [2]


def test_scanner_detects_renamed_any_import() -> None:
    uses = _scan("from typing import Any as Anything\ny: Anything = 1\n")
    assert _lines(uses) == [2]


def test_scanner_detects_renamed_import_under_type_checking_used_earlier() -> None:
    """A renamed import is recognised wherever it sits, even after its use."""
    uses = _scan(
        "from typing import TYPE_CHECKING\n"
        "def f(x: 'Q') -> None: ...\n"
        "if TYPE_CHECKING:\n"
        "    from typing import Any as Q\n"
    )
    assert _lines(uses) == [2]


def test_scanner_ignores_unrelated_any_attribute() -> None:
    """``something.Any`` on a non-typing object is not the typing escape."""
    assert _scan("import enum\nx = enum.Any\n") == []


def test_scanner_detects_any_nested_in_subscript() -> None:
    uses = _scan("from typing import Any\ndef f(d: dict[str, list[Any]]) -> None:\n    ...\n")
    assert _lines(uses) == [2]


def test_scanner_detects_return_and_star_args() -> None:
    uses = _scan(
        "from typing import Any\ndef f(\n    *args: Any,\n    **kwargs: Any,\n) -> Any:\n    ...\n"
    )
    assert _lines(uses) == [3, 4, 5]


def test_scanner_detects_string_annotation() -> None:
    uses = _scan("from typing import Any\nx: 'dict[str, Any]' = {}\n")
    assert _lines(uses) == [2]


def test_scanner_detects_string_nested_inside_an_annotation() -> None:
    uses = _scan("from typing import Any\nx: list['Any'] = []\n")
    assert _lines(uses) == [2]


def test_scanner_detects_type_parameter_bound() -> None:
    uses = _scan(
        "from typing import Any\ndef f[T: Any](x: T) -> T: ...\ndef g[T: 'Any']() -> None: ...\n"
    )
    assert _lines(uses) == [2, 3]


def test_scanner_counts_each_reference_on_a_line() -> None:
    uses = _scan(
        "from collections.abc import Coroutine\n"
        "from typing import Any\n"
        "c: Coroutine[Any, Any, int]\n"
    )
    assert _lines(uses) == [3, 3]


def test_scanner_counts_non_annotation_cast() -> None:
    """``cast(Any, x)`` is a typing escape — counted (see module docstring)."""
    uses = _scan("from typing import Any, cast\ny = cast(Any, 1)\n")
    assert _lines(uses) == [2]


def test_scanner_detects_string_cast() -> None:
    uses = _scan("from typing import cast\ny = cast('Any', 1)\nz = cast('dict[str, Any]', 1)\n")
    assert _lines(uses) == [2, 3]


def test_scanner_detects_typevar_string_bound_and_constraints() -> None:
    uses = _scan(
        "from typing import TypeVar\nT = TypeVar('T', bound='Any')\nU = TypeVar('U', 'Any', int)\n"
    )
    assert _lines(uses) == [2, 3]


def test_scanner_does_not_parse_the_typevar_name() -> None:
    """``TypeVar("Any")`` names a variable; its first argument is not a type."""
    assert _scan("from typing import TypeVar\nAny_ = TypeVar('Any')\n") == []


def test_scanner_detects_assert_type_and_newtype_strings() -> None:
    uses = _scan(
        "from typing import NewType, assert_type\nassert_type(x, 'Any')\nN = NewType('N', 'Any')\n"
    )
    assert _lines(uses) == [2, 3]


def test_scanner_skips_literal_values_and_annotated_metadata() -> None:
    """``Literal["Any"]`` and ``Annotated[int, "Any"]`` hold values, not types."""
    uses = _scan(
        "from typing import Annotated, Literal\n"
        "x: Literal['Any'] = 'Any'\n"
        "y: Annotated[int, 'Any'] = 1\n"
    )
    assert uses == []


def test_scanner_ignores_docstrings_strings_and_comments() -> None:
    uses = _scan(
        "def f() -> None:\n"
        '    """Accepts Any value; typing.Any is banned."""\n'
        "    msg = 'Any: not a type'\n"
        "    # Any mention in a comment is prose\n"
        "    print('Any')\n"
    )
    assert uses == []


def test_scanner_accepts_justification_combined_with_other_comments() -> None:
    uses = _scan(
        "from typing import Any\n"
        "x: Any = 1  # type: ignore[misc]  # Any: pydantic before-validator input\n"
        "y: Any = 2  # noqa: ANN401 # Any: PEP 562 module __getattr__\n"
    )
    assert [u.justified for u in uses] == [True, True]


def test_scanner_rejects_empty_reason() -> None:
    uses = _scan("from typing import Any\nx: Any = 1  # Any:\ny: Any = 2  # Any:   \n")
    assert [u.justified for u in uses] == [False, False]


def test_scanner_justification_must_be_on_the_reference_line() -> None:
    """A reason on a neighbouring line does not cover a multi-line annotation."""
    uses = _scan(
        "from typing import Any\n"
        "def f(  # Any: this comment is on the wrong line\n"
        "    x: Any,\n"
        ") -> None:\n"
        "    ...\n"
    )
    assert [(u.line, u.justified) for u in uses] == [(3, False)]


def test_scanner_justification_inside_a_string_does_not_count() -> None:
    uses = _scan("from typing import Any\nx: Any = '# Any: smuggled in a string'\n")
    assert [u.justified for u in uses] == [False]


@pytest.mark.parametrize(
    "source",
    [
        "from typing import Any\nA = Any  # Any: alias\n",
        "import typing\nA = typing.Any  # Any: alias\n",
        "from typing import Any, TypeAlias\nA: TypeAlias = Any  # Any: alias\n",
        "from typing import TypeAlias\nA: TypeAlias = 'Any'  # Any: alias\n",
        "from typing import TypeAlias\nX: TypeAlias = 'dict[str, Any]'  # Any: alias\n",
        "from typing import Any\ntype A = Any  # Any: alias\n",
        "from typing import Any\ntype Node = dict[str, Any]  # Any: alias\n",
        "from typing import Any\n_Snap = dict[str, Any]  # Any: alias\n",
        "from typing import Any\nMaybe = Any | None  # Any: alias\n",
        "from typing import Any, TypeAliasType\nA = TypeAliasType('A', Any)  # Any: alias\n",
    ],
    ids=[
        "assign",
        "assign-attribute",
        "typealias",
        "typealias-string",
        "typealias-nested-string",
        "type-statement",
        "type-statement-nested",
        "old-style-generic",
        "old-style-union",
        "typealiastype-call",
    ],
)
def test_scanner_flags_aliases_even_when_justified(source: str) -> None:
    """Aliasing launders the count: flagged however well it is justified."""
    uses = _scan(source)
    assert uses, "the aliased Any was not seen at all"
    assert all(use.justified for use in uses)
    assert all(use.in_alias for use in uses)


def test_scanner_computed_values_and_annotations_are_not_aliases() -> None:
    """``x = cast(Any, y)`` and ``kwargs: dict[str, Any] = ...`` alias nothing."""
    uses = _scan(
        "from typing import Any, cast\n"
        "x = cast(Any, 1)\n"
        "kwargs: dict[str, Any] = dict(a=1)\n"
        "def f(y: Any) -> Any: ...\n"
    )
    assert len(uses) == 4
    assert not any(use.in_alias for use in uses)


def test_scanner_refuses_unparseable_source() -> None:
    with pytest.raises(SyntaxError):
        _scan("def f(:\n")
