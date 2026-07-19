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

from niwaki.query import (
    FilterExpr,
    allbit,
    and_,
    anybit,
    bw,
    eq,
    ge,
    gt,
    le,
    lt,
    ne,
    not_,
    or_,
    raw,
    wcard,
    xor,
)
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


class TestFilterValueEscaping:
    """A double-quote in a value is escaped so it can't break the
    ``eq(prop,"...")`` filter grammar (audit Q1)."""

    def test_coerce_value_escapes_double_quote(self) -> None:
        assert _coerce_value('a"b') == 'a\\"b'

    def test_eq_escapes_double_quote(self) -> None:
        assert str(eq("name", 'a"b', cls_name="fvBD")) == 'eq(fvBD.name,"a\\"b")'

    def test_wcard_escapes_double_quote_keeps_wildcard(self) -> None:
        assert str(wcard("descr", 'x"y*', cls_name="fvBD")) == 'wcard(fvBD.descr,"x\\"y*")'


# ── Bitmask operators ─────────────────────────────────────────────────────────


class TestBitmaskOperators:
    def test_anybit_wire_string(self) -> None:
        assert (
            str(anybit("tcpRules", "syn,ack", cls_name="vzEntry"))
            == 'anybit(vzEntry.tcpRules,"syn,ack")'
        )

    def test_allbit_compiles_to_and_of_anybits(self) -> None:
        # No native `allbit` on the APIC — "all bits set" = and(anybit,anybit).
        assert str(allbit("tcpRules", "syn,ack", cls_name="vzEntry")) == (
            'and(anybit(vzEntry.tcpRules,"syn"),anybit(vzEntry.tcpRules,"ack"))'
        )

    def test_allbit_single_flag_is_just_anybit(self) -> None:
        assert (
            str(allbit("scope", "public", cls_name="fvSubnet")) == 'anybit(fvSubnet.scope,"public")'
        )

    def test_allbit_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one flag"):
            allbit("fvSubnet.scope", "")

    def test_anybit_single_flag(self) -> None:
        assert (
            str(anybit("scope", "public", cls_name="fvSubnet")) == 'anybit(fvSubnet.scope,"public")'
        )

    def test_anybit_from_set_canonical_order(self) -> None:
        # A set of names is joined in the enum's canonical order — sorted for
        # plain strings — so the wire form is deterministic.
        assert str(anybit("scope", {"shared", "public"}, cls_name="fvSubnet")) == (
            'anybit(fvSubnet.scope,"public,shared")'
        )

    def test_anybit_from_list_preserves_order(self) -> None:
        assert str(anybit("scope", ["public", "shared"], cls_name="fvSubnet")) == (
            'anybit(fvSubnet.scope,"public,shared")'
        )

    def test_allbit_from_frozenset(self) -> None:
        assert str(allbit("scope", frozenset({"public", "shared"}), cls_name="fvSubnet")) == (
            'and(anybit(fvSubnet.scope,"public"),anybit(fvSubnet.scope,"shared"))'
        )

    def test_anybit_already_qualified(self) -> None:
        assert str(anybit("vzEntry.tcpRules", "syn")) == 'anybit(vzEntry.tcpRules,"syn")'

    def test_anybit_from_enum_set_uses_declaration_order(self) -> None:
        from niwaki.models._generated.enums.FvRouteScp import FvRouteScp

        # A set of enum members joins in the enum's declaration order (bit
        # weight), not alphabetically: PUBLIC is declared before PRIVATE, so
        # "public,private" — never the alphabetical "private,public".
        result = str(anybit("fvSubnet.scope", {FvRouteScp.PRIVATE, FvRouteScp.PUBLIC}))
        assert result == 'anybit(fvSubnet.scope,"public,private")'

    def test_allbit_from_enum_list_preserves_given_order(self) -> None:
        from niwaki.models._generated.enums.FvRouteScp import FvRouteScp

        result = str(allbit("fvSubnet.scope", [FvRouteScp.PRIVATE, FvRouteScp.PUBLIC]))
        assert result == 'and(anybit(fvSubnet.scope,"private"),anybit(fvSubnet.scope,"public"))'


# ── XOR ───────────────────────────────────────────────────────────────────────


class TestXor:
    def test_xor_two(self) -> None:
        a = eq("name", "web")
        b = eq("name", "app")
        assert str(xor(a, b)) == f"xor({a},{b})"

    def test_xor_three(self) -> None:
        a = eq("name", "web")
        b = eq("name", "app")
        c = eq("name", "db")
        assert str(xor(a, b, c)) == f"xor({a},{b},{c})"

    def test_xor_requires_two(self) -> None:
        with pytest.raises(ValueError, match="at least 2"):
            xor(eq("name", "web"))


# ── Raw escape hatch ──────────────────────────────────────────────────────────


class TestRaw:
    def test_raw_is_verbatim(self) -> None:
        assert str(raw('anybit(vzEntry.tcpRules,"syn")')) == 'anybit(vzEntry.tcpRules,"syn")'

    def test_raw_not_escaped(self) -> None:
        # Raw is the verbatim escape hatch — it is not re-escaped.
        assert str(raw("true()")) == "true()"

    def test_raw_composes_with_operators(self) -> None:
        combined = raw("true()") & eq("name", "web")
        assert str(combined) == 'and(true(),eq(name,"web"))'

    def test_filterexpr_str_constructor_is_verbatim(self) -> None:
        # Backward-compat: a raw string passed to FilterExpr is taken as-is.
        assert str(FilterExpr('eq(fvBD.name,"web")')) == 'eq(fvBD.name,"web")'


# ── AST composition of the new operators ──────────────────────────────────────


class TestNewOperatorComposition:
    def test_new_ops_compose_with_and_or_not(self) -> None:
        expr = and_(
            anybit("vzEntry.tcpRules", "syn"),
            or_(wcard("fvBD.name", "web*"), bw("fvBD.pcTag", "1", "100")),
        )
        s = str(expr)
        assert s.startswith("and(")
        assert 'anybit(vzEntry.tcpRules,"syn")' in s
        assert 'wcard(fvBD.name,"web*")' in s
        assert 'bw(fvBD.pcTag,"1","100")' in s

    def test_bitmask_negation(self) -> None:
        assert str(~anybit("vzEntry.tcpRules", "syn")) == 'not(anybit(vzEntry.tcpRules,"syn"))'


# ── Keyword-value wrappers ────────────────────────────────────────────────────


class TestValueWrappers:
    def test_any_of_requires_a_value(self) -> None:
        from niwaki.query import any_of

        with pytest.raises(ValueError, match="at least one value"):
            any_of()
