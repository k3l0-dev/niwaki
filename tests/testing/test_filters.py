"""``query-target-filter`` is evaluated — every operator the SDK renders.

Every filter is built by the SDK's own builder (``where(...)`` keywords and
the operator functions of :mod:`niwaki.query`), so the fake is pinned on the
exact strings the SDK sends, quoting and escaping included.

Comparisons are **semantic**, as measured on a 6.0(9c) APIC:

- a property typed as a number compares numerically (``lt(from_, 20)``
  matches ``9`` and ``10``, not ``100`` — a string compare would);
- a named number matches by value: a filter on ``80`` matches the stored
  ``"http"`` (and a filter on ``"http"`` matches it too);
- a bitmask compares as a set: ``eq(scope, "shared,public")`` matches a
  stored ``"public,shared"``; ``anybit`` tests membership;
- a boolean compares as a boolean: ``where(unicastRoute=True)`` (rendered
  ``"yes"``) matches a BD the design pushed as ``"true"``.
"""

from __future__ import annotations

import pytest

from niwaki import Niwaki
from niwaki.design import tenant
from niwaki.models.fv.fvBD import fvBD
from niwaki.query import (
    FilterExpr,
    FilterValue,
    allbit,
    and_,
    anybit,
    between,
    bw,
    eq,
    ge,
    gt,
    le,
    like,
    lt,
    ne,
    not_,
    or_,
    wcard,
    xor,
)
from niwaki.testing import FakeApic


def _names(found: list[fvBD]) -> list[str]:
    return [bd.dn for bd in found]


@pytest.fixture
def bds(fake: FakeApic) -> FakeApic:
    """Three bridge domains in one tenant, with strings worth escaping."""
    fake.seed(
        {
            "fvTenant": {
                "attributes": {"dn": "uni/tn-t", "name": "t"},
                "children": [
                    {"fvBD": {"attributes": {"name": "prod-a", "arpFlood": "yes"}}},
                    {
                        "fvBD": {
                            "attributes": {
                                "name": "prod-b",
                                "arpFlood": "yes",
                                "descr": 'say "hi"',
                            }
                        }
                    },
                    {"fvBD": {"attributes": {"name": "dev-a", "arpFlood": "no", "descr": "a\\b"}}},
                ],
            }
        }
    )
    return fake


@pytest.fixture
def blocks(fake: FakeApic) -> FakeApic:
    """Node blocks whose integer ``from_`` sorts differently as text and as numbers."""
    for name, start in (("b9", "9"), ("b10", "10"), ("b100", "100"), ("b101", "101")):
        fake.seed(
            {
                "infraNodeBlk": {
                    "attributes": {
                        "dn": f"uni/infra/nprof-p/leaves-s-typ-range/nodeblk-{name}",
                        "name": name,
                        "from_": start,
                        "to_": start,
                    }
                }
            }
        )
    return fake


@pytest.fixture
def subnets(fake: FakeApic) -> FakeApic:
    """Subnets whose bitmask scope covers none, one, and two shared flags."""
    for ip, scope in (
        ("10.0.1.1/24", "public,shared"),
        ("10.0.2.1/24", "private"),
        ("10.0.3.1/24", "public"),
        ("10.0.4.1/24", "private,shared"),
    ):
        fake.seed(
            {
                "fvSubnet": {
                    "attributes": {"dn": f"uni/tn-t/BD-b/subnet-[{ip}]", "ip": ip, "scope": scope}
                }
            }
        )
    return fake


@pytest.fixture
def entries(fake: FakeApic) -> FakeApic:
    """Filter entries whose ports are stored by name (``http``) or by number."""
    for name, port in (("e1", "http"), ("e2", "https"), ("e3", "8080")):
        fake.seed(
            {
                "vzEntry": {
                    "attributes": {
                        "dn": f"uni/tn-t/flt-f/e-{name}",
                        "name": name,
                        "etherT": "ip",
                        "prot": "tcp",
                        "dFromPort": port,
                        "dToPort": port,
                    }
                }
            }
        )
    return fake


def _block_names(aci: Niwaki, *exprs: FilterExpr) -> list[str]:
    found = aci.query("infraNodeBlk").where(*exprs).fetch()
    return sorted(str(mo["name"]) for mo in found)


def _subnet_ips(aci: Niwaki, *exprs: FilterExpr, **kwargs: FilterValue) -> list[str]:
    found = aci.query("fvSubnet").where(*exprs, **kwargs).fetch()
    return sorted(str(mo["ip"]) for mo in found)


class TestEquality:
    def test_keyword_equality(self, bds: FakeApic, aci: Niwaki) -> None:
        assert _names(aci.query(fvBD).where(name="dev-a").fetch()) == ["uni/tn-t/BD-dev-a"]

    def test_explicit_eq_and_ne(self, bds: FakeApic, aci: Niwaki) -> None:
        assert _names(aci.query(fvBD).where(eq("fvBD.name", "prod-a")).fetch()) == [
            "uni/tn-t/BD-prod-a"
        ]
        assert _names(aci.query(fvBD).where(ne("fvBD.name", "prod-a")).fetch()) == [
            "uni/tn-t/BD-dev-a",
            "uni/tn-t/BD-prod-b",
        ]

    def test_an_escaped_quote_is_matched_verbatim(self, bds: FakeApic, aci: Niwaki) -> None:
        found = aci.query(fvBD).where(eq("fvBD.descr", 'say "hi"')).fetch()
        assert _names(found) == ["uni/tn-t/BD-prod-b"]

    def test_an_escaped_backslash_is_matched_verbatim(self, bds: FakeApic, aci: Niwaki) -> None:
        found = aci.query(fvBD).where(eq("fvBD.descr", "a\\b")).fetch()
        assert _names(found) == ["uni/tn-t/BD-dev-a"]

    def test_membership_keyword_is_an_or(self, bds: FakeApic, aci: Niwaki) -> None:
        found = aci.query(fvBD).where(name=["dev-a", "prod-b", "ghost"]).fetch()
        assert _names(found) == ["uni/tn-t/BD-dev-a", "uni/tn-t/BD-prod-b"]

    def test_a_filter_combines_with_scoping(self, bds: FakeApic, aci: Niwaki) -> None:
        fake_other = aci.query(fvBD).under("uni/tn-other").where(name="dev-a").fetch()
        assert fake_other == []
        scoped = aci.query(fvBD).under("uni/tn-t").where(name="dev-a").fetch()
        assert _names(scoped) == ["uni/tn-t/BD-dev-a"]


class TestWildcard:
    def test_trailing_wildcard_keyword(self, bds: FakeApic, aci: Niwaki) -> None:
        found = aci.query(fvBD).where(name="prod-*").fetch()
        assert _names(found) == ["uni/tn-t/BD-prod-a", "uni/tn-t/BD-prod-b"]

    def test_explicit_wcard_and_like(self, bds: FakeApic, aci: Niwaki) -> None:
        assert _names(aci.query(fvBD).where(wcard("fvBD.name", "dev*")).fetch()) == [
            "uni/tn-t/BD-dev-a"
        ]
        assert _names(aci.query(fvBD).where(name=like("prod-b*")).fetch()) == ["uni/tn-t/BD-prod-b"]


class TestNumbers:
    def test_ordering_operators_compare_numerically(self, blocks: FakeApic, aci: Niwaki) -> None:
        assert _block_names(aci, lt("infraNodeBlk.from_", 20)) == ["b10", "b9"]
        assert _block_names(aci, le("infraNodeBlk.from_", 10)) == ["b10", "b9"]
        assert _block_names(aci, gt("infraNodeBlk.from_", 100)) == ["b101"]
        assert _block_names(aci, ge("infraNodeBlk.from_", 100)) == ["b100", "b101"]

    def test_between_is_inclusive_and_numeric(self, blocks: FakeApic, aci: Niwaki) -> None:
        assert _block_names(aci, bw("infraNodeBlk.from_", 10, 100)) == ["b10", "b100"]
        found = aci.query("infraNodeBlk").where(from_=between(9, 10)).fetch()
        assert sorted(str(mo["name"]) for mo in found) == ["b10", "b9"]


class TestNamedNumbers:
    def test_a_number_matches_the_name_it_is_stored_under(
        self, entries: FakeApic, aci: Niwaki
    ) -> None:
        found = aci.query("vzEntry").where(dFromPort=80).fetch()
        assert [mo.dn for mo in found] == ["uni/tn-t/flt-f/e-e1"]

    def test_the_name_matches_too(self, entries: FakeApic, aci: Niwaki) -> None:
        found = aci.query("vzEntry").where(eq("vzEntry.dFromPort", "https")).fetch()
        assert [mo.dn for mo in found] == ["uni/tn-t/flt-f/e-e2"]

    def test_an_unnamed_number_matches_as_a_number(self, entries: FakeApic, aci: Niwaki) -> None:
        found = aci.query("vzEntry").where(dFromPort=8080).fetch()
        assert [mo.dn for mo in found] == ["uni/tn-t/flt-f/e-e3"]

    def test_a_design_push_is_found_by_its_number(self, fake: FakeApic, aci: Niwaki) -> None:
        tenant("t").filter("f").entry("web", tcp=80).push(aci)
        found = aci.query("vzEntry").where(dFromPort=80).fetch()
        assert [mo.dn for mo in found] == ["uni/tn-t/flt-f/e-web"]


class TestBitmasks:
    def test_set_equality_ignores_the_order(self, subnets: FakeApic, aci: Niwaki) -> None:
        assert _subnet_ips(aci, scope={"shared", "public"}) == ["10.0.1.1/24"]
        assert _subnet_ips(aci, eq("fvSubnet.scope", "shared,public")) == ["10.0.1.1/24"]

    def test_set_equality_is_not_a_subset_test(self, subnets: FakeApic, aci: Niwaki) -> None:
        assert _subnet_ips(aci, eq("fvSubnet.scope", "public")) == ["10.0.3.1/24"]

    def test_anybit_tests_membership(self, subnets: FakeApic, aci: Niwaki) -> None:
        assert _subnet_ips(aci, anybit("fvSubnet.scope", "shared")) == [
            "10.0.1.1/24",
            "10.0.4.1/24",
        ]
        assert _subnet_ips(aci, anybit("fvSubnet.scope", {"public", "private"})) == [
            "10.0.1.1/24",
            "10.0.2.1/24",
            "10.0.3.1/24",
            "10.0.4.1/24",
        ]

    def test_allbit_emulation_requires_every_flag(self, subnets: FakeApic, aci: Niwaki) -> None:
        assert _subnet_ips(aci, allbit("fvSubnet.scope", {"public", "shared"})) == ["10.0.1.1/24"]


class TestBooleans:
    def test_a_boolean_keyword_matches_the_stored_spelling(
        self, bds: FakeApic, aci: Niwaki
    ) -> None:
        assert _names(aci.query(fvBD).where(arpFlood=False).fetch()) == ["uni/tn-t/BD-dev-a"]

    def test_a_design_true_is_found_by_a_filter_yes(self, fake: FakeApic, aci: Niwaki) -> None:
        tenant("t").bd("web", unicast_routing=True).push(aci)
        assert _names(aci.query(fvBD).where(unicastRoute=True).fetch()) == ["uni/tn-t/BD-web"]
        assert aci.query(fvBD).where(unicastRoute=False).fetch() == []


class TestLogic:
    def test_and_or_not(self, bds: FakeApic, aci: Niwaki) -> None:
        both = and_(wcard("fvBD.name", "prod-*"), eq("fvBD.descr", 'say "hi"'))
        assert _names(aci.query(fvBD).where(both).fetch()) == ["uni/tn-t/BD-prod-b"]
        either = or_(eq("fvBD.name", "prod-a"), eq("fvBD.name", "dev-a"))
        assert _names(aci.query(fvBD).where(either).fetch()) == [
            "uni/tn-t/BD-dev-a",
            "uni/tn-t/BD-prod-a",
        ]
        assert _names(aci.query(fvBD).where(not_(wcard("fvBD.name", "prod-*"))).fetch()) == [
            "uni/tn-t/BD-dev-a"
        ]

    def test_operators_compose(self, bds: FakeApic, aci: Niwaki) -> None:
        expr = (eq("fvBD.arpFlood", True) & ~eq("fvBD.name", "prod-a")) | eq("fvBD.name", "dev-a")
        assert _names(aci.query(fvBD).where(expr).fetch()) == [
            "uni/tn-t/BD-dev-a",
            "uni/tn-t/BD-prod-b",
        ]

    def test_xor_keeps_exactly_one_side(self, bds: FakeApic, aci: Niwaki) -> None:
        # prod-a: both true; prod-b: only the flood side; dev-a: neither.
        expr = xor(eq("fvBD.name", "prod-a"), eq("fvBD.arpFlood", True))
        assert _names(aci.query(fvBD).where(expr).fetch()) == ["uni/tn-t/BD-prod-b"]

    def test_chained_where_is_a_conjunction(self, bds: FakeApic, aci: Niwaki) -> None:
        found = aci.query(fvBD).where(name="prod-*").where(descr='say "hi"').fetch()
        assert _names(found) == ["uni/tn-t/BD-prod-b"]
