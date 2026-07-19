"""ACI filter expression DSL.

Provides typed :class:`FilterExpr` objects that serialise to APIC filter strings.

APIC filters address properties as ``class.prop`` (the wire names).  The
``where(prop=value)`` keyword shorthand qualifies the property with the queried
class for you; **explicit operator expressions do not** — qualify the property
yourself (``eq("fvBD.name", "web")``) or pass ``cls_name="fvBD"``.  Property
names that already contain a dot are treated as qualified and passed through
unchanged.

Internally every expression is a small **render-once AST** (the private
``_Compare`` / ``_Bits`` / ``_Between`` / ``_Logical`` / ``_Raw``
nodes).  A single node renders to the wire string in exactly one place — the
only spot that quotes and escapes a value — so a stray ``"`` can never break the
``op(prop,"...")`` grammar, and a future reader (filter validation, typed field
expressions) has a structured tree to inspect rather than an opaque string.

Operator reference
------------------
=================  ============================================
Operator           APIC filter function
=================  ============================================
``eq``             ``eq(cls.prop,"value")``    — equal
``ne``             ``ne(cls.prop,"value")``    — not equal
``lt``             ``lt(cls.prop,"value")``    — less than
``le``             ``le(cls.prop,"value")``    — less or equal
``gt``             ``gt(cls.prop,"value")``    — greater than
``ge``             ``ge(cls.prop,"value")``    — greater or equal
``wcard``          ``wcard(cls.prop,"pat*")``  — wildcard match (trailing ``*``)
``bw``             ``bw(cls.prop,"a","b")``    — between (inclusive)
``anybit``         ``anybit(cls.prop,"a,b")``  — any bit of the mask set
``allbit``         *and(anybit…) sugar*        — all bits set (no native op)
``and_``           ``and(expr1,expr2,...)``    — logical AND
``or_``            ``or(expr1,expr2,...)``     — logical OR
``xor``            ``xor(expr1,expr2,...)``    — logical XOR
``not_``           ``not(expr)``               — logical NOT
``raw``            *verbatim*                  — escape hatch (any operator)
=================  ============================================

Example::

    from niwaki.query import eq, wcard, and_

    # Qualified property names:
    expr = and_(wcard("fvBD.name", "prod-*"), eq("fvBD.arpFlood", True))
    # → 'and(wcard(fvBD.name,"prod-*"),eq(fvBD.arpFlood,"yes"))'

    # Same thing via cls_name=:
    expr = eq("name", "web", cls_name="fvBD")
    # → 'eq(fvBD.name,"web")'

    # Operator chaining via &, |, ~:
    expr = eq("fvBD.name", "web") & eq("fvBD.arpFlood", True)
    # → 'and(eq(fvBD.name,"web"),eq(fvBD.arpFlood,"yes"))'

    # Bitmask filtering (a Flags field):
    from niwaki.query import anybit
    expr = anybit("vzEntry.tcpRules", {"syn", "ack"})
    # → 'anybit(vzEntry.tcpRules,"ack,syn")'
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from niwaki.models._wire import to_filter, to_wire

# ── Filter AST ────────────────────────────────────────────────────────────────


def _escape(text: str) -> str:
    """Escape the double-quote that would otherwise break ``op(prop,"...")``.

    A raw ``"`` inside a filter value yields a malformed or mis-parsed filter
    (audit Q1).  This is the single point where a filter value is escaped.

    Args:
        text: A rendered wire value.

    Returns:
        The value with every ``"`` backslash-escaped.
    """
    return text.replace('"', '\\"')


@dataclass(frozen=True, slots=True)
class _Compare:
    """A binary comparison — ``op(prop,"value")``.  *value* is coerced+escaped."""

    op: str
    prop: str
    value: str

    def render(self) -> str:
        return f'{self.op}({self.prop},"{self.value}")'


@dataclass(frozen=True, slots=True)
class _Bits:
    """A bitmask test — ``anybit(prop,"a,b")`` / ``allbit(prop,"a,b")``."""

    op: str
    prop: str
    flags: str

    def render(self) -> str:
        return f'{self.op}({self.prop},"{self.flags}")'


@dataclass(frozen=True, slots=True)
class _Between:
    """An inclusive range — ``bw(prop,"lo","hi")``.  Bounds are coerced+escaped."""

    prop: str
    lo: str
    hi: str

    def render(self) -> str:
        return f'bw({self.prop},"{self.lo}","{self.hi}")'


@dataclass(frozen=True, slots=True)
class _Logical:
    """A logical combinator — ``and`` / ``or`` / ``xor`` (n-ary) or ``not`` (unary)."""

    op: str
    operands: tuple[_FilterNode, ...]

    def render(self) -> str:
        return f"{self.op}({','.join(node.render() for node in self.operands)})"


@dataclass(frozen=True, slots=True)
class _Raw:
    """A verbatim filter string — the escape hatch for operators not modelled."""

    text: str

    def render(self) -> str:
        return self.text


type _FilterNode = _Compare | _Bits | _Between | _Logical | _Raw


class FilterExpr:
    """An APIC filter expression backed by a small render-once AST.

    Created by the operator functions (:func:`eq`, :func:`ne`, :func:`wcard`,
    …).  Combines with ``&`` (AND), ``|`` (OR), and ``~`` (NOT) for ergonomic
    composition.  ``str(expr)`` renders the wire filter string.

    A raw string may still be passed for backward compatibility and as a
    verbatim escape hatch (identical to :func:`raw`) — it is rendered exactly as
    given, with no further escaping.

    Args:
        node: An internal filter AST node, or a raw APIC filter string
            (e.g. ``'eq(fvBD.name,"web")'``) taken verbatim.

    Example::

        from niwaki.query import eq, wcard

        expr = eq("fvBD.name", "web")
        ~expr            # → FilterExpr('not(eq(fvBD.name,"web"))')
        expr & wcard("fvBD.name", "prod-*")  # → FilterExpr('and(...)')
    """

    __slots__ = ("_node",)

    def __init__(self, node: _FilterNode | str) -> None:
        self._node: _FilterNode = _Raw(node) if isinstance(node, str) else node

    def __str__(self) -> str:
        return self._node.render()

    def __repr__(self) -> str:
        return f"FilterExpr({self._node.render()!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, FilterExpr):
            return self._node.render() == other._node.render()
        return NotImplemented

    def __and__(self, other: FilterExpr) -> FilterExpr:
        """Combine two expressions with logical AND (``&``)."""
        return and_(self, other)

    def __or__(self, other: FilterExpr) -> FilterExpr:
        """Combine two expressions with logical OR (``|``)."""
        return or_(self, other)

    def __invert__(self) -> FilterExpr:
        """Negate this expression (``~``)."""
        return not_(self)


# ── Internal helpers ──────────────────────────────────────────────────────────


def _coerce_value(value: Any) -> str:
    """Convert a Python value to its APIC filter string representation.

    Delegates to the wire boundary (:mod:`niwaki.models._wire`) so a filter
    speaks exactly what the APIC stores — a filter that renders a value
    differently from the way it was written can never match — then escapes the
    double-quote that would break the ``op(prop,"...")`` grammar (audit Q1).

    Args:
        value: Python value — ``bool``, ``int``, ``str``, an enum member, or a
            set of flags.

    Returns:
        APIC string: ``True`` → ``"yes"``, ``False`` → ``"no"``, a set of flags
        → its canonical comma-joined form, everything else as it is stored.
    """
    return _escape(to_filter(value))


def _qualify(prop: str, cls_name: str) -> str:
    """Return *prop* prefixed with *cls_name* unless already qualified.

    A property is considered already-qualified when it contains a dot.

    Args:
        prop:     Property name (e.g. ``"name"`` or ``"fvBD.name"``).
        cls_name: ACI class name to prepend (e.g. ``"fvBD"``).

    Returns:
        Qualified property string.

    Example::

        _qualify("name", "fvBD")       → "fvBD.name"
        _qualify("fvBD.name", "fvBD")  → "fvBD.name"  (no double-prefix)
        _qualify("name", "")           → "name"
    """
    if "." in prop or not cls_name:
        return prop
    return f"{cls_name}.{prop}"


def _join_flags(flags: str | Enum | Iterable[str | Enum]) -> str:
    """Render a set/list/single flag (or wire string) as the APIC comma form.

    Args:
        flags: The comma-joined wire string (``"syn,ack"``), a single flag
            (member or name), or an iterable of members/names.

    Returns:
        The comma-joined wire string the APIC expects inside ``anybit``/
        ``allbit`` (e.g. ``"syn,ack"``).  A set is joined in the enum's canonical
        order; a string is taken as-is.
    """
    if isinstance(flags, str):
        return flags
    if isinstance(flags, Enum):
        return to_wire(flags)
    if isinstance(flags, frozenset | set):
        return to_wire(flags)
    return ",".join(to_wire(flag) for flag in flags)


def _flag_list(flags: str | Enum | Iterable[str | Enum]) -> list[str]:
    """The individual wire flags of a bitmask value (``"a,b"`` → ``["a", "b"]``)."""
    return [flag for flag in _join_flags(flags).split(",") if flag]


def _compare(op: str, prop: str, value: Any, cls_name: str) -> FilterExpr:
    """Build a binary-comparison expression (shared by eq/ne/lt/le/gt/ge)."""
    return FilterExpr(_Compare(op, _qualify(prop, cls_name), _coerce_value(value)))


# ── Public operator functions ─────────────────────────────────────────────────


def eq(prop: str, value: Any, *, cls_name: str = "") -> FilterExpr:
    """Equal: ``eq(cls.prop,"value")``.

    Args:
        prop:     Property name.  Qualify it yourself (``"fvBD.name"``) or
                  provide *cls_name* — an unqualified property is not a valid
                  APIC filter.
        value:    Comparison value.  ``bool`` values are coerced to APIC
                  ``"yes"``/``"no"``; all others via the wire boundary.
        cls_name: ACI class name prepended to *prop* when it is not already
                  qualified.

    Returns:
        :class:`FilterExpr` representing the equality check.

    Example::

        eq("name", "web", cls_name="fvBD")
        # → FilterExpr('eq(fvBD.name,"web")')
    """
    return _compare("eq", prop, value, cls_name)


def ne(prop: str, value: Any, *, cls_name: str = "") -> FilterExpr:
    """Not equal: ``ne(cls.prop,"value")``.

    Args:
        prop:     Property name.
        value:    Comparison value.
        cls_name: ACI class name for auto-prefix.

    Returns:
        :class:`FilterExpr`.
    """
    return _compare("ne", prop, value, cls_name)


def lt(prop: str, value: Any, *, cls_name: str = "") -> FilterExpr:
    """Less than: ``lt(cls.prop,"value")``.

    Args:
        prop:     Property name.
        value:    Comparison value.
        cls_name: ACI class name for auto-prefix.

    Returns:
        :class:`FilterExpr`.
    """
    return _compare("lt", prop, value, cls_name)


def le(prop: str, value: Any, *, cls_name: str = "") -> FilterExpr:
    """Less or equal: ``le(cls.prop,"value")``.

    Args:
        prop:     Property name.
        value:    Comparison value.
        cls_name: ACI class name for auto-prefix.

    Returns:
        :class:`FilterExpr`.
    """
    return _compare("le", prop, value, cls_name)


def gt(prop: str, value: Any, *, cls_name: str = "") -> FilterExpr:
    """Greater than: ``gt(cls.prop,"value")``.

    Args:
        prop:     Property name.
        value:    Comparison value.
        cls_name: ACI class name for auto-prefix.

    Returns:
        :class:`FilterExpr`.
    """
    return _compare("gt", prop, value, cls_name)


def ge(prop: str, value: Any, *, cls_name: str = "") -> FilterExpr:
    """Greater or equal: ``ge(cls.prop,"value")``.

    Args:
        prop:     Property name.
        value:    Comparison value.
        cls_name: ACI class name for auto-prefix.

    Returns:
        :class:`FilterExpr`.
    """
    return _compare("ge", prop, value, cls_name)


def wcard(prop: str, pattern: str, *, cls_name: str = "") -> FilterExpr:
    """Wildcard match: ``wcard(cls.prop,"prod-*")``.

    The APIC supports ``*`` as a glob wildcard, but it validates the pattern
    against the property's own format, which has two consequences (both verified
    live on 6.0(9c)): a **leading** ``*`` is rejected on most properties
    (``wcard(name,"prod*")`` works, ``wcard(name,"*prod*")`` → HTTP 301), so
    prefer a trailing wildcard; and strictly-formatted properties (``ip``,
    ``dn``, ``descr``) reject any wildcard value. The pattern is taken as-is —
    only a literal ``"`` is escaped — so wildcards are preserved.

    Args:
        prop:     Property name.
        pattern:  Glob pattern, preferably prefix + trailing ``*`` (``"prod-*"``).
        cls_name: ACI class name for auto-prefix.

    Returns:
        :class:`FilterExpr`.

    Example::

        wcard("name", "prod-*", cls_name="fvBD")
        # → FilterExpr('wcard(fvBD.name,"prod-*")')
    """
    return FilterExpr(_Compare("wcard", _qualify(prop, cls_name), _escape(pattern)))


def bw(prop: str, start: Any, end: Any, *, cls_name: str = "") -> FilterExpr:
    """Between (inclusive): ``bw(cls.prop,"start","end")``.

    Args:
        prop:     Property name.
        start:    Lower bound (inclusive).
        end:      Upper bound (inclusive).
        cls_name: ACI class name for auto-prefix.

    Returns:
        :class:`FilterExpr`.
    """
    qprop = _qualify(prop, cls_name)
    return FilterExpr(_Between(qprop, _coerce_value(start), _coerce_value(end)))


def anybit(
    prop: str, flags: str | Enum | Iterable[str | Enum], *, cls_name: str = ""
) -> FilterExpr:
    """Any-bit bitmask test: ``anybit(cls.prop,"syn,ack")``.

    True when *at least one* of the given flags is set in the property's
    bitmask.  This is the read counterpart of a ``Flags`` field: ``eq`` matches
    the whole mask exactly, ``anybit`` matches on a single bit regardless of the
    others.

    Args:
        prop:     Property name of a bitmask (``Flags``) attribute.
        flags:    The flags to test — a comma-joined wire string
                  (``"syn,ack"``), a single flag, or an iterable of members/names.
        cls_name: ACI class name for auto-prefix.

    Returns:
        :class:`FilterExpr`.

    Example::

        anybit("vzEntry.tcpRules", {"syn", "ack"})
        # → FilterExpr('anybit(vzEntry.tcpRules,"ack,syn")')
    """
    return FilterExpr(_Bits("anybit", _qualify(prop, cls_name), _escape(_join_flags(flags))))


def allbit(
    prop: str, flags: str | Enum | Iterable[str | Enum], *, cls_name: str = ""
) -> FilterExpr:
    """All-bit bitmask test — every given flag must be set.

    The APIC 6.0 filter grammar has **no** ``allbit`` operator (verified live:
    ``allbit(...)`` → HTTP 301 "no such filter type"), so this compiles to a
    conjunction of :func:`anybit` tests — ``and(anybit(prop,a),anybit(prop,b))``
    — which is exactly "bit *a* set AND bit *b* set".

    Args:
        prop:     Property name of a bitmask (``Flags``) attribute.
        flags:    The flags that must all be set — a comma-joined wire string
                  (``"syn,ack"``), a single flag, or an iterable of members/names.
        cls_name: ACI class name for auto-prefix.

    Returns:
        :class:`FilterExpr`.

    Raises:
        ValueError: No flags were given.
    """
    members = _flag_list(flags)
    if not members:
        raise ValueError("allbit() requires at least one flag")
    parts = [anybit(prop, member, cls_name=cls_name) for member in members]
    return parts[0] if len(parts) == 1 else and_(*parts)


def and_(*exprs: FilterExpr) -> FilterExpr:
    """Logical AND of two or more expressions: ``and(e1,e2,...)``.

    Args:
        *exprs: Two or more :class:`FilterExpr` objects to combine.

    Returns:
        :class:`FilterExpr` representing the conjunction.

    Raises:
        ValueError: Fewer than two expressions provided.

    Example::

        and_(eq("name", "web"), eq("arpFlood", True))
        # → FilterExpr('and(eq(name,"web"),eq(arpFlood,"yes"))')
    """
    if len(exprs) < 2:
        raise ValueError(f"and_() requires at least 2 expressions, got {len(exprs)}")
    return FilterExpr(_Logical("and", tuple(expr._node for expr in exprs)))


def or_(*exprs: FilterExpr) -> FilterExpr:
    """Logical OR of two or more expressions: ``or(e1,e2,...)``.

    Args:
        *exprs: Two or more :class:`FilterExpr` objects to combine.

    Returns:
        :class:`FilterExpr` representing the disjunction.

    Raises:
        ValueError: Fewer than two expressions provided.

    Example::

        or_(wcard("name", "prod-*"), wcard("name", "dev-*"))
        # → FilterExpr('or(wcard(name,"prod-*"),wcard(name,"dev-*"))')
    """
    if len(exprs) < 2:
        raise ValueError(f"or_() requires at least 2 expressions, got {len(exprs)}")
    return FilterExpr(_Logical("or", tuple(expr._node for expr in exprs)))


def xor(*exprs: FilterExpr) -> FilterExpr:
    """Logical XOR of two or more expressions: ``xor(e1,e2,...)``.

    Args:
        *exprs: Two or more :class:`FilterExpr` objects to combine.

    Returns:
        :class:`FilterExpr` representing the exclusive-or.

    Raises:
        ValueError: Fewer than two expressions provided.
    """
    if len(exprs) < 2:
        raise ValueError(f"xor() requires at least 2 expressions, got {len(exprs)}")
    return FilterExpr(_Logical("xor", tuple(expr._node for expr in exprs)))


def not_(expr: FilterExpr) -> FilterExpr:
    """Logical NOT: ``not(expr)``.

    Args:
        expr: :class:`FilterExpr` to negate.

    Returns:
        :class:`FilterExpr` representing the negation.

    Example::

        not_(eq("name", "infra"))
        # → FilterExpr('not(eq(name,"infra"))')
    """
    return FilterExpr(_Logical("not", (expr._node,)))


def raw(text: str) -> FilterExpr:
    """Wrap a verbatim APIC filter string — the universal escape hatch.

    Every operator the SDK does not model (``true``, ``false``, ``pholder``,
    ``passive``, or any future addition) is reachable this way: the string is
    embedded exactly as given, with no coercion or escaping, and composes with
    ``&``/``|``/``~`` like any other expression.

    Args:
        text: A raw APIC filter expression (e.g. ``'true()'``).

    Returns:
        :class:`FilterExpr` wrapping *text* verbatim.
    """
    return FilterExpr(_Raw(text))


# ── Keyword-argument value helpers ────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _AnyOf:
    """A ``where(prop=any_of(...))`` tag — match any of several values."""

    values: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class _Like:
    """A ``where(prop=like("*pat*"))`` tag — an explicit wildcard match."""

    pattern: str


@dataclass(frozen=True, slots=True)
class _Range:
    """A ``where(prop=between(a, b))`` tag — an inclusive range."""

    start: Any
    end: Any


type _ValueTag = _AnyOf | _Like | _Range

type _FilterScalar = str | int | float | bool | Enum

type FilterValue = (
    _FilterScalar
    | frozenset[_FilterScalar]
    | set[_FilterScalar]
    | list[_FilterScalar]
    | tuple[_FilterScalar, ...]
    | _ValueTag
)
"""What a ``where(prop=value)`` keyword accepts.

The value's *type* chooses the operator: a scalar is an equality, a ``list`` /
``tuple`` (or :func:`any_of`) is a membership OR, a ``str`` containing ``*`` (or
:func:`like`) is a wildcard, a ``set`` / ``frozenset`` is a bitmask equality (for
``Flags`` fields), and :func:`between` is an inclusive range.
"""


def any_of(*values: _FilterScalar) -> _AnyOf:
    """Match a property against any of several values — a membership OR.

    ``where(code=any_of("F0467", "F1394"))`` is the explicit form of
    ``where(code=["F0467", "F1394"])``; both compile to ``or(eq(...),eq(...))``
    (the APIC filter grammar has no ``in``).

    Args:
        *values: One or more values to match.

    Returns:
        A tag consumed by :meth:`~niwaki.query.Query.where`.

    Raises:
        ValueError: No values were given.
    """
    if not values:
        raise ValueError("any_of() requires at least one value")
    return _AnyOf(tuple(values))


def like(pattern: str) -> _Like:
    """Match a property against a wildcard pattern — the explicit form of a glob.

    ``where(name=like("prod-*"))`` is the explicit form of
    ``where(name="prod-*")``; use it to force a wildcard where the value would
    otherwise read as a literal.

    Args:
        pattern: A glob pattern (``*`` wildcard).

    Returns:
        A tag consumed by :meth:`~niwaki.query.Query.where`.
    """
    return _Like(pattern)


def between(start: _FilterScalar, end: _FilterScalar) -> _Range:
    """Match a property within an inclusive range — ``bw(prop,"start","end")``.

    ``where(pri=between(1, 5))`` compiles to ``bw(cls.pri,"1","5")``.

    Args:
        start: Lower bound (inclusive).
        end:   Upper bound (inclusive).

    Returns:
        A tag consumed by :meth:`~niwaki.query.Query.where`.
    """
    return _Range(start, end)


def _membership(prop: str, values: tuple[Any, ...], cls_name: str) -> FilterExpr:
    """Compile a collection of values into an OR of equalities (the APIC has no ``in``)."""
    if not values:
        raise ValueError(f"where({prop}=[]): an empty collection matches nothing — omit it")
    if any(value is None for value in values):
        raise ValueError(
            f"where({prop}=...): None is not a valid filter value (the APIC has no NULL)"
        )
    equalities = [eq(prop, value, cls_name=cls_name) for value in values]
    return equalities[0] if len(equalities) == 1 else or_(*equalities)


def _kwarg_to_expr(prop: str, value: Any, cls_name: str) -> FilterExpr:
    """Map a ``where(prop=value)`` keyword to a filter expression by the value's type.

    The dispatch that turns the ergonomic keyword form into the APIC filter
    grammar; see :data:`FilterValue` for the rules.  A ``set``/``frozenset`` is
    deliberately kept as a bitmask equality (a ``Flags`` field), *not* a
    membership OR — only ``list``/``tuple`` mean "any of".

    Raises:
        TypeError: *value* is a :class:`FilterExpr` (pass those positionally).
        ValueError: *value* is ``None`` or an empty collection.
    """
    if isinstance(value, FilterExpr):
        raise TypeError(
            f"where({prop}=<FilterExpr>): pass an operator expression positionally, "
            "e.g. where(eq(...)), not as a keyword value"
        )
    if isinstance(value, _AnyOf):
        return _membership(prop, value.values, cls_name)
    if isinstance(value, _Like):
        return wcard(prop, value.pattern, cls_name=cls_name)
    if isinstance(value, _Range):
        return bw(prop, value.start, value.end, cls_name=cls_name)
    if value is None:
        raise ValueError(
            f"where({prop}=None): None is not a valid filter value (the APIC has no NULL)"
        )
    if isinstance(value, list | tuple):
        return _membership(prop, tuple(value), cls_name)
    if isinstance(value, str) and "*" in value:
        return wcard(prop, value, cls_name=cls_name)
    # set / frozenset → eq (a Flags bitmask), and every scalar → eq.
    return eq(prop, value, cls_name=cls_name)
