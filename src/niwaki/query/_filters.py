"""ACI filter expression DSL.

Provides typed :class:`FilterExpr` objects that serialise to APIC filter strings.

APIC filters address properties as ``class.prop`` (the wire names).  The
``where(prop=value)`` keyword shorthand qualifies the property with the queried
class for you; **explicit operator expressions do not** — qualify the property
yourself (``eq("fvBD.name", "web")``) or pass ``cls_name="fvBD"``.  Property
names that already contain a dot are treated as qualified and passed through
unchanged.

Operator reference
------------------
=================  ============================================
Operator           APIC filter function
=================  ============================================
``eq``             ``eq(cls.prop,"value")``   — equal
``ne``             ``ne(cls.prop,"value")``   — not equal
``lt``             ``lt(cls.prop,"value")``   — less than
``le``             ``le(cls.prop,"value")``   — less or equal
``gt``             ``gt(cls.prop,"value")``   — greater than
``ge``             ``ge(cls.prop,"value")``   — greater or equal
``wcard``          ``wcard(cls.prop,"*pat*")`` — wildcard match
``bw``             ``bw(cls.prop,"a","b")``   — between
``and_``           ``and(expr1,expr2,...)``   — logical AND
``or_``            ``or(expr1,expr2,...)``    — logical OR
``not_``           ``not(expr)``              — logical NOT
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
"""

from __future__ import annotations

from typing import Any

from niwaki.models._wire import to_filter


class FilterExpr:
    """An APIC filter expression.

    Created by the operator functions (:func:`eq`, :func:`ne`, :func:`wcard`,
    …).  Combines with ``&`` (AND), ``|`` (OR), and ``~`` (NOT) for ergonomic
    composition.

    Args:
        expr: Raw APIC filter string (e.g. ``'eq(fvBD.name,"web")'``).

    Example::

        from niwaki.query import eq, wcard

        expr = eq("fvBD.name", "web")
        ~expr            # → FilterExpr('not(eq(fvBD.name,"web"))')
        expr & wcard("fvBD.name", "prod-*")  # → FilterExpr('and(...)')
    """

    __slots__ = ("_expr",)

    def __init__(self, expr: str) -> None:
        self._expr = expr

    def __str__(self) -> str:
        return self._expr

    def __repr__(self) -> str:
        return f"FilterExpr({self._expr!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, FilterExpr):
            return self._expr == other._expr
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

    Delegates to the wire boundary (:mod:`niwaki.models._wire`), so a filter
    speaks exactly what the APIC stores — a filter that renders a value
    differently from the way it was written can never match.

    Args:
        value: Python value — ``bool``, ``int``, ``str``, an enum member, or a
            set of flags.

    Returns:
        APIC string: ``True`` → ``"yes"``, ``False`` → ``"no"``, a set of flags
        → its canonical comma-joined form, everything else as it is stored.
    """
    # Escape the double-quote that would otherwise break the eq(prop,"...")
    # filter grammar (a raw " in a value yields a malformed or mis-parsed
    # filter — audit Q1).
    return to_filter(value).replace('"', '\\"')


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


def _binary_op(op: str, prop: str, value: Any, cls_name: str) -> FilterExpr:
    qprop = _qualify(prop, cls_name)
    return FilterExpr(f'{op}({qprop},"{_coerce_value(value)}")')


# ── Public operator functions ─────────────────────────────────────────────────


def eq(prop: str, value: Any, *, cls_name: str = "") -> FilterExpr:
    """Equal: ``eq(cls.prop,"value")``.

    Args:
        prop:     Property name.  Qualify it yourself (``"fvBD.name"``) or
                  provide *cls_name* — an unqualified property is not a valid
                  APIC filter.
        value:    Comparison value.  ``bool`` values are coerced to APIC
                  ``"yes"``/``"no"``; all others via ``str()``.
        cls_name: ACI class name prepended to *prop* when it is not already
                  qualified.

    Returns:
        :class:`FilterExpr` representing the equality check.

    Example::

        eq("name", "web", cls_name="fvBD")
        # → FilterExpr('eq(fvBD.name,"web")')
    """
    return _binary_op("eq", prop, value, cls_name)


def ne(prop: str, value: Any, *, cls_name: str = "") -> FilterExpr:
    """Not equal: ``ne(cls.prop,"value")``.

    Args:
        prop:     Property name.
        value:    Comparison value.
        cls_name: ACI class name for auto-prefix.

    Returns:
        :class:`FilterExpr`.
    """
    return _binary_op("ne", prop, value, cls_name)


def lt(prop: str, value: Any, *, cls_name: str = "") -> FilterExpr:
    """Less than: ``lt(cls.prop,"value")``.

    Args:
        prop:     Property name.
        value:    Comparison value.
        cls_name: ACI class name for auto-prefix.

    Returns:
        :class:`FilterExpr`.
    """
    return _binary_op("lt", prop, value, cls_name)


def le(prop: str, value: Any, *, cls_name: str = "") -> FilterExpr:
    """Less or equal: ``le(cls.prop,"value")``.

    Args:
        prop:     Property name.
        value:    Comparison value.
        cls_name: ACI class name for auto-prefix.

    Returns:
        :class:`FilterExpr`.
    """
    return _binary_op("le", prop, value, cls_name)


def gt(prop: str, value: Any, *, cls_name: str = "") -> FilterExpr:
    """Greater than: ``gt(cls.prop,"value")``.

    Args:
        prop:     Property name.
        value:    Comparison value.
        cls_name: ACI class name for auto-prefix.

    Returns:
        :class:`FilterExpr`.
    """
    return _binary_op("gt", prop, value, cls_name)


def ge(prop: str, value: Any, *, cls_name: str = "") -> FilterExpr:
    """Greater or equal: ``ge(cls.prop,"value")``.

    Args:
        prop:     Property name.
        value:    Comparison value.
        cls_name: ACI class name for auto-prefix.

    Returns:
        :class:`FilterExpr`.
    """
    return _binary_op("ge", prop, value, cls_name)


def wcard(prop: str, pattern: str, *, cls_name: str = "") -> FilterExpr:
    """Wildcard match: ``wcard(cls.prop,"prod-*")``.

    The APIC supports ``*`` as a glob wildcard.

    Args:
        prop:     Property name.
        pattern:  Glob pattern (e.g. ``"prod-*"``, ``"*-bd"``, ``"*web*"``).
        cls_name: ACI class name for auto-prefix.

    Returns:
        :class:`FilterExpr`.

    Example::

        wcard("name", "prod-*", cls_name="fvBD")
        # → FilterExpr('wcard(fvBD.name,"prod-*")')
    """
    qprop = _qualify(prop, cls_name)
    return FilterExpr(f'wcard({qprop},"{pattern.replace(chr(34), chr(92) + chr(34))}")')


def bw(prop: str, start: Any, end: Any, *, cls_name: str = "") -> FilterExpr:
    """Between: ``bw(cls.prop,"start","end")``.

    Args:
        prop:     Property name.
        start:    Lower bound (inclusive).
        end:      Upper bound (inclusive).
        cls_name: ACI class name for auto-prefix.

    Returns:
        :class:`FilterExpr`.
    """
    qprop = _qualify(prop, cls_name)
    return FilterExpr(f'bw({qprop},"{_coerce_value(start)}","{_coerce_value(end)}")')


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
    return FilterExpr(f"and({','.join(str(e) for e in exprs)})")


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
    return FilterExpr(f"or({','.join(str(e) for e in exprs)})")


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
    return FilterExpr(f"not({expr})")
