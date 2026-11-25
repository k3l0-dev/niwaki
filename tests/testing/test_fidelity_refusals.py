"""Refuse what cannot be honoured — the fidelity boundaries of the fake.

The guiding rule: never a silently wrong answer.  Where the fake would have
to *guess* what an APIC does (an order it does not know, a name it cannot
resolve, semantics nobody measured, a placement that contradicts itself), it
answers an APIC error whose text starts with ``"FakeApic:"`` — through the
SDK, an :class:`~niwaki.exceptions.APIError` with that ``apic_message``; from
``seed()``, a ``ValueError`` with that message.

Contract pinned here (decisions recorded for the implementer):

- **Ordering (M1).**  ``lt le gt ge bw`` and ``order-by`` are served on
  ``int`` / ``float`` properties (numerically), on ``str`` properties (as
  text) and, for ``order-by`` only, on booleans (false before true).  They
  are refused on ``enum``, ``ip`` and ``mac`` properties, whose fabric order
  the fake does not know.  Equality stays served on every kind.
- **Unresolvable named numbers (M2).**  A property of a class with no
  generated model whose catalogue kind is ``int`` and whose catalogue lists
  value names, but for which the fake has no number → name table
  (``actrlEntry.dFromPort``), cannot be stored or compared by value.  The
  boundary chosen: writing a *numeric* value to it (POST or seed) is refused,
  a *name* is stored verbatim; in a filter, ``eq``/``ne`` with a name literal
  are served, ``eq``/``ne`` with a numeric literal are refused, and every
  ordering (``lt le gt ge bw``, ``order-by``) is refused.
- **Out-of-scope class terms (M3).**  A filter term ``op(X.prop, …)`` applies
  to class ``X`` and its concrete subclasses.  It is refused when ``X`` lies
  outside the classes the filter can meet (the query class, or the
  ``target-subtree-class`` list — each with its concrete subclasses; for
  ``rsp-subtree-filter``, the ``rsp-subtree-class`` list), and — since the
  APIC's semantics for a term met by an object of another class were never
  measured — whenever it is evaluated against such an object (an ``also()``
  query, an unrestricted subtree).  ``ne(X.p,v)`` and ``not(eq(X.p,v))``
  therefore behave the same: both refused.
- **Wildcards (M4).**  ``wcard`` with a leading ``*`` is refused, and any
  ``wcard`` on ``fvSubnet.ip``-like ``ip`` properties, on ``dn`` and on
  ``descr`` is refused (measured live on 6.0(9c): HTTP 301).  A trailing
  glob on other properties stays served.
- **Placement (M5).**  A posted object whose RN names the same class as the
  URL's last RN must *be* the URL's object (``fvBD name=y`` posted at
  ``…/BD-x`` is refused, never stored as ``…/BD-x/BD-y``).  An explicit
  ``dn`` must be the URL DN or lie under it, must end in the RN the naming
  properties compute (naming properties absent from the payload are taken
  from the ``dn``; a ``dn`` whose RN does not fit the class is refused), and
  a child's explicit ``dn`` must sit directly under its parent.  A class the
  catalogue does not know is refused by POST and by ``seed()``.
- **Response-subtree parameters (minors).**  ``rsp-subtree-class`` and
  ``rsp-subtree-filter`` without an embedding ``rsp-subtree`` are refused;
  ``rsp-subtree-include=required`` with nothing to require (no
  ``rsp-subtree``, no facet) is refused — ``faults,required`` (the SDK's
  ``with_faults().only_faulted()``) stays served.  An ``order-by`` property
  qualified with a class the query cannot return is refused.
- **Client construction (minor).**  When the client constructor refuses an
  option, ``client()`` / ``async_client()`` leave no unclosed ``httpx``
  client behind.
"""

from __future__ import annotations

from collections.abc import Mapping

import httpx
import pytest

from niwaki import Niwaki
from niwaki.exceptions import APIError
from niwaki.models.fv.fvBD import fvBD
from niwaki.query import (
    FilterExpr,
    bw,
    eq,
    ge,
    gt,
    le,
    like,
    lt,
    ne,
    not_,
    wcard,
)
from niwaki.testing import FakeApic
from niwaki.transport import ApicSession
from niwaki.types import JsonObject

from .conftest import error_text, request

ACTRL = "topology/pod-1/node-101/sys/actrl/filt-5"


def _refused(caught: pytest.ExceptionInfo[APIError], *fragments: str) -> None:
    message = caught.value.apic_message
    assert message.startswith("FakeApic:"), message
    for fragment in fragments:
        assert fragment in message, message


def _fetch_refused(aci: Niwaki, cls: str, *exprs: FilterExpr, fragment: str = "") -> None:
    with pytest.raises(APIError) as caught:
        aci.query(cls).where(*exprs).fetch()
    _refused(caught, *([fragment] if fragment else []))


# ── M1 — ordering ─────────────────────────────────────────────────────────────


@pytest.fixture
def ordered(fake: FakeApic) -> FakeApic:
    """Objects carrying enum, ip, mac, int, str and bool properties."""
    fake.seed(
        {
            "fvTenant": {
                "attributes": {"dn": "uni/tn-t", "name": "t"},
                "children": [
                    {
                        "fvBD": {
                            "attributes": {"name": "web", "mac": "00:22:BD:F8:19:FF"},
                            "children": [
                                {"fvSubnet": {"attributes": {"ip": "10.0.1.1/24"}}},
                                {"fvSubnet": {"attributes": {"ip": "10.0.9.1/24"}}},
                            ],
                        }
                    },
                    {"fvBD": {"attributes": {"name": "db", "mac": "00:22:BD:F8:19:FE"}}},
                    {
                        "fvAp": {
                            "attributes": {"name": "shop"},
                            "children": [
                                {"fvAEPg": {"attributes": {"name": "a", "prio": "level1"}}},
                                {"fvAEPg": {"attributes": {"name": "b", "prio": "level3"}}},
                            ],
                        }
                    },
                ],
            }
        }
    )
    for name, start in (("b9", "9"), ("b10", "10"), ("b100", "100")):
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


class TestOrdering:
    @pytest.mark.parametrize(
        ("cls", "expr"),
        [
            pytest.param("fvAEPg", lt("fvAEPg.prio", "level2"), id="enum-lt"),
            pytest.param("fvAEPg", ge("fvAEPg.prio", "level2"), id="enum-ge"),
            pytest.param("fvAEPg", bw("fvAEPg.prio", "level1", "level3"), id="enum-bw"),
            pytest.param("fvSubnet", gt("fvSubnet.ip", "10.0.5.1/24"), id="ip-gt"),
            pytest.param("fvSubnet", bw("fvSubnet.ip", "10.0.0.0/8", "10.0.5.1/24"), id="ip-bw"),
            pytest.param("fvBD", le("fvBD.mac", "00:22:BD:F8:19:FE"), id="mac-le"),
        ],
    )
    def test_comparisons_without_a_known_order_are_refused(
        self, ordered: FakeApic, aci: Niwaki, cls: str, expr: FilterExpr
    ) -> None:
        _fetch_refused(aci, cls, expr)

    @pytest.mark.parametrize(
        ("cls", "prop"),
        [("fvAEPg", "prio"), ("fvSubnet", "ip"), ("fvBD", "mac")],
    )
    def test_order_by_without_a_known_order_is_refused(
        self, ordered: FakeApic, aci: Niwaki, cls: str, prop: str
    ) -> None:
        with pytest.raises(APIError) as caught:
            aci.query(cls).order_by(prop).fetch()
        _refused(caught, prop)

    def test_equality_stays_served_on_every_kind(self, ordered: FakeApic, aci: Niwaki) -> None:
        assert [mo.dn for mo in aci.query("fvAEPg").where(prio="level3").fetch()] == [
            "uni/tn-t/ap-shop/epg-b"
        ]
        found = aci.query("fvSubnet").where(eq("fvSubnet.ip", "10.0.9.1/24")).fetch()
        assert [mo.dn for mo in found] == ["uni/tn-t/BD-web/subnet-[10.0.9.1/24]"]
        found_bd = aci.query(fvBD).where(ne("fvBD.mac", "00:22:BD:F8:19:FF")).fetch()
        assert [bd.dn for bd in found_bd] == ["uni/tn-t/BD-db"]

    def test_numbers_and_text_stay_ordered(self, ordered: FakeApic, aci: Niwaki) -> None:
        by_number = aci.query("infraNodeBlk").order_by("from_", desc=True).fetch()
        assert [str(mo["name"]) for mo in by_number] == ["b100", "b10", "b9"]
        text = aci.query(fvBD).where(lt("fvBD.name", "m")).fetch()
        assert [bd.dn for bd in text] == ["uni/tn-t/BD-db"]
        by_name = aci.query(fvBD).order_by("name").fetch()
        assert [bd.dn for bd in by_name] == ["uni/tn-t/BD-db", "uni/tn-t/BD-web"]


# ── M2 — named numbers the fake cannot resolve ────────────────────────────────


def _actrl_entry(name: str, port: str) -> JsonObject:
    return {
        "actrlEntry": {"attributes": {"dn": f"{ACTRL}/ent-{name}", "name": name, "dFromPort": port}}
    }


@pytest.fixture
def actrl(fake: FakeApic) -> FakeApic:
    fake.seed(_actrl_entry("e1", "http"))
    fake.seed(_actrl_entry("e2", "https"))
    return fake


class TestUnresolvableNamedNumbers:
    def test_a_name_is_stored_verbatim(self, actrl: FakeApic) -> None:
        stored = actrl.get(f"{ACTRL}/ent-e1")
        assert stored is not None
        assert stored.attributes["dFromPort"] == "http"

    @pytest.mark.parametrize("port", ["80", "8080"])
    def test_seeding_a_number_is_refused(self, fake: FakeApic, port: str) -> None:
        with pytest.raises(ValueError, match=r"FakeApic:.*dFromPort"):
            fake.seed(_actrl_entry("e9", port))
        assert fake.get(f"{ACTRL}/ent-e9") is None

    def test_posting_a_number_is_refused(self, fake: FakeApic) -> None:
        body = {"actrlEntry": {"attributes": {"name": "e9", "dFromPort": "80"}}}
        response = fake.handle(request("POST", f"/api/mo/{ACTRL}/ent-e9.json", body))
        assert response.status_code == 400
        assert error_text(response).startswith("FakeApic:")
        assert "dFromPort" in error_text(response)
        assert fake.get(f"{ACTRL}/ent-e9") is None

    def test_equality_on_a_name_is_served(self, actrl: FakeApic, aci: Niwaki) -> None:
        found = aci.query("actrlEntry").where(eq("actrlEntry.dFromPort", "http")).fetch()
        assert [mo.dn for mo in found] == [f"{ACTRL}/ent-e1"]
        others = aci.query("actrlEntry").where(ne("actrlEntry.dFromPort", "http")).fetch()
        assert [mo.dn for mo in others] == [f"{ACTRL}/ent-e2"]

    @pytest.mark.parametrize(
        "expr",
        [
            eq("actrlEntry.dFromPort", "80"),
            ne("actrlEntry.dFromPort", "80"),
            lt("actrlEntry.dFromPort", "https"),
            gt("actrlEntry.dFromPort", "100"),
            bw("actrlEntry.dFromPort", "1", "1024"),
        ],
    )
    def test_a_numeric_literal_or_an_ordering_is_refused(
        self, actrl: FakeApic, aci: Niwaki, expr: FilterExpr
    ) -> None:
        _fetch_refused(aci, "actrlEntry", expr, fragment="dFromPort")

    def test_order_by_is_refused(self, actrl: FakeApic, aci: Niwaki) -> None:
        with pytest.raises(APIError) as caught:
            aci.query("actrlEntry").order_by("dFromPort").fetch()
        _refused(caught, "dFromPort")


# ── M3 — terms qualified with another class ───────────────────────────────────


class TestOutOfScopeTerms:
    def test_a_class_query_refuses_another_class_term(self, aci: Niwaki) -> None:
        """Refused before anything is read — even against an empty store."""
        _fetch_refused(aci, "fvBD", eq("fvSubnet.ip", "10.0.1.1/24"), fragment="fvSubnet")

    def test_a_subclass_term_is_in_scope(self, fake: FakeApic, aci: Niwaki) -> None:
        fake.seed({"fvAEPg": {"attributes": {"dn": "uni/tn-t/ap-a/epg-web", "name": "web"}}})
        found = aci.query("fvAEPg").where(eq("fvEPg.name", "web")).fetch()
        assert [mo.dn for mo in found] == ["uni/tn-t/ap-a/epg-web"]

    def test_a_target_subtree_class_list_bounds_the_terms(
        self, seeded: FakeApic, session: ApicSession
    ) -> None:
        with pytest.raises(APIError) as caught:
            session.get(
                "/api/mo/uni/tn-prod.json",
                **{
                    "query-target": "subtree",
                    "target-subtree-class": "fvBD",
                    "query-target-filter": 'eq(fvSubnet.ip,"10.0.1.1/24")',
                },
            )
        _refused(caught, "fvSubnet")

    @pytest.mark.parametrize(
        "expr",
        [
            eq("fvBD.name", "web"),
            ne("fvBD.name", "web"),
            not_(eq("fvBD.name", "web")),
        ],
    )
    def test_a_multi_class_query_refuses_a_term_met_by_another_class(
        self, seeded: FakeApic, aci: Niwaki, expr: FilterExpr
    ) -> None:
        with pytest.raises(APIError) as caught:
            aci.query(fvBD).under("uni/tn-prod/BD-web").also("fvSubnet").where(expr).fetch()
        _refused(caught, "fvBD")

    def test_an_unrestricted_subtree_refuses_a_term_met_by_another_class(
        self, seeded: FakeApic, session: ApicSession
    ) -> None:
        with pytest.raises(APIError) as caught:
            session.get(
                "/api/mo/uni/tn-prod/BD-web.json",
                **{"query-target": "subtree", "query-target-filter": 'eq(fvBD.name,"web")'},
            )
        _refused(caught, "fvBD")

    def test_a_subtree_filter_is_bounded_by_the_subtree_classes(
        self, seeded: FakeApic, aci: Niwaki
    ) -> None:
        with pytest.raises(APIError) as caught:
            (
                aci.query(fvBD)
                .include("fvSubnet")
                .subtree_where(eq("fvRsCtx.tnFvCtxName", "main"))
                .fetch()
            )
        _refused(caught, "fvRsCtx")

    def test_an_unrestricted_subtree_filter_refuses_another_class_child(
        self, seeded: FakeApic, session: ApicSession
    ) -> None:
        with pytest.raises(APIError) as caught:
            session.get(
                "/api/mo/uni/tn-prod/BD-web.json",
                **{
                    "rsp-subtree": "children",
                    "rsp-subtree-filter": 'eq(fvSubnet.ip,"10.0.1.1/24")',
                },
            )
        _refused(caught, "fvSubnet")


# ── M4 — wildcards the APIC refuses ───────────────────────────────────────────


class TestWildcards:
    @pytest.mark.parametrize(
        ("cls", "expr", "fragment"),
        [
            ("fvBD", wcard("fvBD.name", "*web"), "wcard"),
            ("fvBD", wcard("fvBD.name", "*eb*"), "wcard"),
            ("fvSubnet", wcard("fvSubnet.ip", "10.0.*"), "fvSubnet.ip"),
            ("fvBD", wcard("fvBD.dn", "uni/tn-prod/*"), "fvBD.dn"),
            ("fvBD", wcard("fvBD.descr", "say*"), "fvBD.descr"),
        ],
    )
    def test_measured_rejections_are_refused(
        self, seeded: FakeApic, aci: Niwaki, cls: str, expr: FilterExpr, fragment: str
    ) -> None:
        _fetch_refused(aci, cls, expr, fragment=fragment)

    def test_a_leading_wildcard_keyword_is_refused(self, seeded: FakeApic, aci: Niwaki) -> None:
        with pytest.raises(APIError) as caught:
            aci.query(fvBD).where(name=like("*eb")).fetch()
        _refused(caught)

    def test_a_trailing_glob_stays_served(self, seeded: FakeApic, aci: Niwaki) -> None:
        found = aci.query(fvBD).where(name="we*").fetch()
        assert [bd.dn for bd in found] == ["uni/tn-dev/BD-web", "uni/tn-prod/BD-web"]


# ── M5 — placement ────────────────────────────────────────────────────────────


def _post_refused(fake: FakeApic, dn: str, body: Mapping[str, object], *fragments: str) -> None:
    before = fake.objects()
    response = fake.handle(request("POST", f"/api/mo/{dn}.json", body))
    assert response.status_code == 400
    text = error_text(response)
    assert text.startswith("FakeApic:"), text
    for fragment in fragments:
        assert fragment in text, text
    assert fake.objects() == before, "a refused POST must change nothing"


class TestPlacement:
    def test_a_sibling_name_at_an_object_url_is_refused(self, fake: FakeApic) -> None:
        _post_refused(fake, "uni/tn-a/BD-x", {"fvBD": {"attributes": {"name": "y"}}})
        assert fake.get("uni/tn-a/BD-x/BD-y") is None

    def test_another_tenant_at_a_tenant_url_is_refused(self, fake: FakeApic) -> None:
        _post_refused(fake, "uni/tn-a", {"fvTenant": {"attributes": {"name": "b"}}})

    def test_a_body_dn_elsewhere_than_the_url_is_refused(self, fake: FakeApic) -> None:
        body = {"fvTenant": {"attributes": {"dn": "uni/tn-b", "name": "b"}}}
        _post_refused(fake, "uni/tn-a", body, "uni/tn-b")

    def test_a_body_dn_disagreeing_with_its_naming_is_refused(self, fake: FakeApic) -> None:
        body = {"fvBD": {"attributes": {"dn": "uni/tn-a/BD-x", "name": "y"}}}
        _post_refused(fake, "uni/tn-a/BD-x", body)

    def test_an_unknown_class_post_is_refused(self, fake: FakeApic) -> None:
        body = {"fooBarNoSuchClass": {"attributes": {"dn": "uni/foo-1"}}}
        _post_refused(fake, "uni/foo-1", body, "fooBarNoSuchClass")

    def test_a_child_at_its_parent_url_stays_accepted(self, fake: FakeApic) -> None:
        body = {"fvBD": {"attributes": {"name": "web"}}}
        assert fake.handle(request("POST", "/api/mo/uni/tn-a.json", body)).status_code == 200
        assert fake.get("uni/tn-a/BD-web") is not None

    def test_seed_refuses_a_dn_disagreeing_with_its_naming(self, fake: FakeApic) -> None:
        with pytest.raises(ValueError, match="FakeApic:"):
            fake.seed({"fvBD": {"attributes": {"dn": "uni/tn-a/BD-x", "name": "y"}}})
        assert fake.get("uni/tn-a/BD-x") is None

    def test_seed_refuses_a_dn_whose_rn_does_not_fit_the_class(self, fake: FakeApic) -> None:
        with pytest.raises(ValueError, match="FakeApic:"):
            fake.seed({"fvBD": {"attributes": {"dn": "uni/tn-a/ctx-x"}}})
        assert fake.get("uni/tn-a/ctx-x") is None

    def test_seed_refuses_a_child_dn_outside_its_parent(self, fake: FakeApic) -> None:
        with pytest.raises(ValueError, match="FakeApic:"):
            fake.seed(
                {
                    "fvTenant": {
                        "attributes": {"dn": "uni/tn-a", "name": "a"},
                        "children": [
                            {"fvBD": {"attributes": {"dn": "uni/tn-b/BD-x", "name": "x"}}}
                        ],
                    }
                }
            )
        assert fake.get("uni/tn-a") is None
        assert fake.get("uni/tn-b/BD-x") is None

    def test_seed_refuses_an_unknown_class(self, fake: FakeApic) -> None:
        with pytest.raises(ValueError, match=r"FakeApic:.*fooBarNoSuchClass"):
            fake.seed({"fooBarNoSuchClass": {"attributes": {"dn": "uni/foo-1"}}})
        assert fake.get("uni/foo-1") is None

    def test_naming_values_absent_from_the_payload_come_from_the_dn(self, fake: FakeApic) -> None:
        fake.seed({"fvTenant": {"attributes": {"dn": "uni/tn-acme"}}})
        stored = fake.get("uni/tn-acme")
        assert stored is not None
        assert stored.attributes["name"] == "acme"

    def test_a_consistent_child_dn_stays_accepted(self, fake: FakeApic) -> None:
        fake.seed(
            {
                "fvTenant": {
                    "attributes": {"dn": "uni/tn-a", "name": "a"},
                    "children": [{"fvBD": {"attributes": {"dn": "uni/tn-a/BD-x", "name": "x"}}}],
                }
            }
        )
        assert fake.get("uni/tn-a/BD-x") is not None


# ── Minors ────────────────────────────────────────────────────────────────────


class TestResponseSubtreeParameters:
    def test_a_subtree_class_list_with_nothing_embedded_is_refused(
        self, seeded: FakeApic, session: ApicSession
    ) -> None:
        with pytest.raises(APIError) as caught:
            session.get("/api/class/fvBD.json", **{"rsp-subtree-class": "fvSubnet"})
        _refused(caught, "rsp-subtree-class")

    def test_a_subtree_filter_with_nothing_embedded_is_refused(
        self, seeded: FakeApic, aci: Niwaki
    ) -> None:
        with pytest.raises(APIError) as caught:
            aci.query(fvBD).subtree_where(eq("fvSubnet.ip", "10.0.1.1/24")).fetch()
        _refused(caught, "rsp-subtree-filter")

    def test_required_with_nothing_to_require_is_refused(
        self, seeded: FakeApic, session: ApicSession
    ) -> None:
        with pytest.raises(APIError) as caught:
            session.get("/api/class/fvBD.json", **{"rsp-subtree-include": "required"})
        _refused(caught, "required")

    def test_only_faulted_stays_served(self, seeded: FakeApic, aci: Niwaki) -> None:
        assert aci.query(fvBD).with_faults().only_faulted().fetch() == []

    def test_order_by_on_another_class_is_refused(self, seeded: FakeApic, aci: Niwaki) -> None:
        with pytest.raises(APIError) as caught:
            aci.query(fvBD).order_by("fvSubnet.descr").fetch()
        _refused(caught, "fvSubnet")


class TestClientConstructionLeavesNothingOpen:
    def test_a_refused_sync_option_closes_the_http_client(
        self, fake: FakeApic, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        created: list[httpx.Client] = []
        original = httpx.Client

        def recording(**kwargs: object) -> httpx.Client:
            client = original(**kwargs)  # pyright: ignore[reportArgumentType]
            created.append(client)
            return client

        monkeypatch.setattr(httpx, "Client", recording)
        with pytest.raises(TypeError):
            fake.client(max_concurrent=3)
        assert all(client.is_closed for client in created)

    def test_a_refused_async_option_leaves_no_open_http_client(
        self, fake: FakeApic, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        created: list[httpx.AsyncClient] = []
        original = httpx.AsyncClient

        def recording(**kwargs: object) -> httpx.AsyncClient:
            client = original(**kwargs)  # pyright: ignore[reportArgumentType]
            created.append(client)
            return client

        monkeypatch.setattr(httpx, "AsyncClient", recording)
        with pytest.raises(TypeError):
            fake.async_client(no_such_option=1)
        assert all(client.is_closed for client in created)
