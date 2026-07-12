"""Tests for the niwaki.query filter expression DSL.

Covers:
- All comparison operators (eq, ne, lt, le, gt, ge, wcard, bw)
- Logical operators (and_, or_, not_)
- Python operator overloads (&, |, ~)
- Auto-prefix with cls_name
- Already-qualified prop pass-through (no double-prefix)
- Bool value coercion (True → "yes", False → "no")
- Error cases (and_/or_ with < 2 args)
"""

from __future__ import annotations

import pytest

from niwaki.query import FilterExpr, and_, bw, eq, ge, gt, le, lt, ne, not_, or_, wcard
from niwaki.query._filters import _coerce_value, _qualify  # type: ignore[reportPrivateUsage]

# ── FilterExpr basics ─────────────────────────────────────────────────────────


class TestFilterExpr:
    def test_str_returns_expression(self) -> None:
        expr = FilterExpr('eq(fvBD.name,"web")')
        assert str(expr) == 'eq(fvBD.name,"web")'

    def test_repr_wraps_in_class_name(self) -> None:
        expr = FilterExpr('eq(x,"y")')
        assert repr(expr) == "FilterExpr('eq(x,\"y\")')"

    def test_equality_same_expr(self) -> None:
        a = FilterExpr('eq(x,"y")')
        b = FilterExpr('eq(x,"y")')
        assert a == b

    def test_equality_different_expr(self) -> None:
        a = FilterExpr('eq(x,"y")')
        b = FilterExpr('ne(x,"y")')
        assert a != b

    def test_and_operator(self) -> None:
        a = eq("name", "web")
        b = eq("arpFlood", True)
        combined = a & b
        assert isinstance(combined, FilterExpr)
        assert str(combined).startswith("and(")

    def test_or_operator(self) -> None:
        a = eq("name", "web")
        b = eq("name", "infra")
        combined = a | b
        assert str(combined).startswith("or(")

    def test_invert_operator(self) -> None:
        a = eq("name", "infra")
        negated = ~a
        assert str(negated).startswith("not(")


# ── Helpers ───────────────────────────────────────────────────────────────────


class TestCoerceValue:
    def test_bool_true(self) -> None:
        assert _coerce_value(True) == "yes"

    def test_bool_false(self) -> None:
        assert _coerce_value(False) == "no"

    def test_int(self) -> None:
        assert _coerce_value(42) == "42"

    def test_str_passthrough(self) -> None:
        assert _coerce_value("web") == "web"


class TestQualify:
    def test_simple_prop_with_cls(self) -> None:
        assert _qualify("name", "fvBD") == "fvBD.name"

    def test_already_qualified_passthrough(self) -> None:
        assert _qualify("fvBD.name", "fvBD") == "fvBD.name"

    def test_no_cls_returns_prop_unchanged(self) -> None:
        assert _qualify("name", "") == "name"

    def test_dotted_prop_with_different_cls_not_double_prefixed(self) -> None:
        assert _qualify("fvBD.name", "other") == "fvBD.name"


# ── Comparison operators ──────────────────────────────────────────────────────


class TestComparisonOperators:
    def test_eq_no_prefix(self) -> None:
        assert str(eq("name", "web")) == 'eq(name,"web")'

    def test_eq_with_cls_name(self) -> None:
        assert str(eq("name", "web", cls_name="fvBD")) == 'eq(fvBD.name,"web")'

    def test_eq_already_qualified(self) -> None:
        assert str(eq("fvBD.name", "web")) == 'eq(fvBD.name,"web")'

    def test_eq_bool_true(self) -> None:
        assert str(eq("arpFlood", True, cls_name="fvBD")) == 'eq(fvBD.arpFlood,"yes")'

    def test_eq_bool_false(self) -> None:
        assert str(eq("unicastRoute", False, cls_name="fvBD")) == 'eq(fvBD.unicastRoute,"no")'

    def test_ne(self) -> None:
        assert str(ne("name", "infra", cls_name="fvBD")) == 'ne(fvBD.name,"infra")'

    def test_lt(self) -> None:
        assert str(lt("pri", "5", cls_name="fvBD")) == 'lt(fvBD.pri,"5")'

    def test_le(self) -> None:
        assert str(le("pri", "5", cls_name="fvBD")) == 'le(fvBD.pri,"5")'

    def test_gt(self) -> None:
        assert str(gt("pri", "0", cls_name="fvBD")) == 'gt(fvBD.pri,"0")'

    def test_ge(self) -> None:
        assert str(ge("pri", "1", cls_name="fvBD")) == 'ge(fvBD.pri,"1")'

    def test_wcard(self) -> None:
        assert str(wcard("name", "prod-*", cls_name="fvBD")) == 'wcard(fvBD.name,"prod-*")'

    def test_wcard_no_cls(self) -> None:
        assert str(wcard("name", "*-bd")) == 'wcard(name,"*-bd")'

    def test_bw(self) -> None:
        assert str(bw("pri", "1", "5", cls_name="fvBD")) == 'bw(fvBD.pri,"1","5")'

    def test_bw_bool_coercion(self) -> None:
        # Verifies coercion is applied to both bounds
        result = str(bw("flag", False, True, cls_name="fvBD"))
        assert '"no"' in result
        assert '"yes"' in result


# ── Logical operators ─────────────────────────────────────────────────────────


class TestLogicalOperators:
    def test_and_two_exprs(self) -> None:
        a = eq("name", "web")
        b = eq("arpFlood", True)
        result = and_(a, b)
        assert str(result) == f"and({a},{b})"

    def test_and_three_exprs(self) -> None:
        a = eq("name", "web")
        b = eq("arpFlood", True)
        c = ne("name", "infra")
        result = and_(a, b, c)
        assert str(result) == f"and({a},{b},{c})"

    def test_and_requires_two(self) -> None:
        with pytest.raises(ValueError, match="at least 2"):
            and_(eq("name", "web"))

    def test_or_two_exprs(self) -> None:
        a = wcard("name", "prod-*")
        b = wcard("name", "dev-*")
        result = or_(a, b)
        assert str(result) == f"or({a},{b})"

    def test_or_requires_two(self) -> None:
        with pytest.raises(ValueError, match="at least 2"):
            or_(eq("name", "web"))

    def test_not(self) -> None:
        a = eq("name", "infra")
        result = not_(a)
        assert str(result) == f"not({a})"

    def test_operator_chaining(self) -> None:
        a = eq("name", "web")
        b = eq("arpFlood", True)
        c = ne("name", "infra")
        result = (a & b) | ~c
        assert str(result).startswith("or(")
        assert "and(" in str(result)
        assert "not(" in str(result)

    def test_nested_and_or(self) -> None:
        expr = and_(
            or_(wcard("name", "prod-*"), wcard("name", "dev-*")),
            eq("arpFlood", True),
        )
        s = str(expr)
        assert s.startswith("and(")
        assert "or(" in s
        assert "wcard(" in s
