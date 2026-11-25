"""The fake's GET grammar — scoping, response subtrees, properties, order and pages.

Exercised through the SDK's own query builder whenever the builder can emit
the parameter (so the fake is pinned on what the SDK really sends), and
through a raw :class:`~niwaki.transport.ApicSession` for the parameter
shapes the builder never produces on its own.

Contract pinned here (decisions recorded for the implementer):

- Every returned item carries its materialised ``dn``.
- Without ``order-by`` the result is in **DN order** — deterministic, so a
  test can assert a list.
- ``totalCount`` is the size of the whole filtered result, whatever page was
  asked for; pages slice the ordered result.
- ``rsp-subtree-filter`` is **evaluated** (it narrows the embedded children);
  ``rsp-subtree-include`` evaluates ``faults`` / ``health`` / ``required``
  against the objects actually stored (a ``faultInst`` child is a fault) and
  fails loud on a facet the fake cannot answer honestly (``audit-logs``: the
  fake keeps no history) — see ``test_fail_loud.py``.
- ``rsp-prop-include=naming-only`` keeps ``dn`` plus the naming properties;
  ``config-only`` keeps ``dn`` plus the properties the catalogue marks
  configurable.
"""

from __future__ import annotations

from niwaki import Niwaki
from niwaki.models.fv.fvBD import fvBD
from niwaki.query import SubtreeInclude
from niwaki.testing import FakeApic
from niwaki.transport import ApicSession

from .conftest import (
    FABRIC,
    attrs_of,
    children_of,
    class_of,
    dns_of,
    envelope,
    imdata,
    request,
)


class TestScoping:
    def test_a_class_query_returns_every_instance_in_dn_order(
        self, seeded: FakeApic, aci: Niwaki
    ) -> None:
        assert [bd.dn for bd in aci.query(fvBD).fetch()] == [
            "uni/tn-dev/BD-web",
            "uni/tn-prod/BD-db",
            "uni/tn-prod/BD-web",
        ]

    def test_a_class_query_returns_typed_models(self, seeded: FakeApic, aci: Niwaki) -> None:
        web = aci.query(fvBD).where(name="web").under("uni/tn-prod").one()
        assert isinstance(web, fvBD)
        assert web.arp_flooding is True

    def test_self_reads_the_object_alone(self, seeded: FakeApic, session: ApicSession) -> None:
        items = session.get("/api/mo/uni/tn-prod.json")
        assert dns_of(items) == ["uni/tn-prod"]
        assert children_of(items[0]) == []

    def test_self_of_a_missing_dn_is_empty(self, seeded: FakeApic, session: ApicSession) -> None:
        assert session.get("/api/mo/uni/tn-ghost.json") == []

    def test_self_only_through_the_builder(self, seeded: FakeApic, aci: Niwaki) -> None:
        found = aci.query(fvBD).under("uni/tn-prod/BD-web").self_only().fetch()
        assert [bd.dn for bd in found] == ["uni/tn-prod/BD-web"]

    def test_subtree_reaches_every_depth(self, seeded: FakeApic, aci: Niwaki) -> None:
        found = aci.query("fvSubnet").under("uni/tn-prod").fetch()
        assert [s.dn for s in found] == [
            "uni/tn-prod/BD-db/subnet-[10.0.3.1/24]",
            "uni/tn-prod/BD-web/subnet-[10.0.1.1/24]",
            "uni/tn-prod/BD-web/subnet-[10.0.2.1/24]",
        ]

    def test_children_stops_at_one_level(self, seeded: FakeApic, aci: Niwaki) -> None:
        assert aci.query("fvSubnet").under("uni/tn-prod").children().fetch() == []
        bds = aci.query(fvBD).under("uni/tn-prod").children().fetch()
        assert [bd.dn for bd in bds] == ["uni/tn-prod/BD-db", "uni/tn-prod/BD-web"]

    def test_children_with_bracketed_rns_are_direct_children(
        self, seeded: FakeApic, session: ApicSession
    ) -> None:
        items = session.get(
            "/api/mo/uni/tn-prod/ap-shop/epg-web.json", **{"query-target": "children"}
        )
        assert sorted(class_of(item) for item in items) == ["fvRsBd", "fvRsPathAtt"]

    def test_children_without_a_class_list_returns_every_class(
        self, seeded: FakeApic, session: ApicSession
    ) -> None:
        items = session.get("/api/mo/uni/tn-prod/BD-web.json", **{"query-target": "children"})
        assert dns_of(items) == [
            "uni/tn-prod/BD-web/rsctx",
            "uni/tn-prod/BD-web/subnet-[10.0.1.1/24]",
            "uni/tn-prod/BD-web/subnet-[10.0.2.1/24]",
        ]

    def test_subtree_includes_the_scope_itself_when_its_class_is_asked(
        self, seeded: FakeApic, session: ApicSession
    ) -> None:
        items = session.get(
            "/api/mo/uni/tn-prod.json",
            **{"query-target": "subtree", "target-subtree-class": "fvTenant,fvCtx"},
        )
        assert dns_of(items) == ["uni/tn-prod", "uni/tn-prod/ctx-main"]

    def test_also_returns_several_classes_in_one_request(
        self, seeded: FakeApic, aci: Niwaki
    ) -> None:
        found = aci.query(fvBD).under("uni/tn-prod/BD-web").also("fvSubnet").fetch()
        assert [mo.dn for mo in found] == [
            "uni/tn-prod/BD-web",
            "uni/tn-prod/BD-web/subnet-[10.0.1.1/24]",
            "uni/tn-prod/BD-web/subnet-[10.0.2.1/24]",
        ]

    def test_every_item_carries_its_dn(self, seeded: FakeApic, session: ApicSession) -> None:
        items = session.get("/api/class/fvSubnet.json")
        assert len(dns_of(items)) == 3

    def test_the_whole_universe_reads_back_what_was_seeded(
        self, fake: FakeApic, session: ApicSession
    ) -> None:
        fake.seed(FABRIC)
        items = session.get("/api/mo/uni.json", **{"query-target": "subtree"})
        assert len(items) == len(fake.objects())


class TestResponseSubtree:
    def test_include_embeds_only_the_named_children(self, seeded: FakeApic, aci: Niwaki) -> None:
        web = aci.query(fvBD).under("uni/tn-prod").where(name="web").include("fvSubnet").one()
        assert sorted(child.dn for child in web.children) == [
            "uni/tn-prod/BD-web/subnet-[10.0.1.1/24]",
            "uni/tn-prod/BD-web/subnet-[10.0.2.1/24]",
        ]

    def test_rsp_subtree_children_embeds_one_level(
        self, seeded: FakeApic, session: ApicSession
    ) -> None:
        (item,) = session.get("/api/mo/uni/tn-prod.json", **{"rsp-subtree": "children"})
        children = children_of(item)
        assert sorted(class_of(c) for c in children) == ["fvAp", "fvBD", "fvBD", "fvCtx"]
        assert all(children_of(c) == [] for c in children)

    def test_rsp_subtree_full_nests_every_depth(
        self, seeded: FakeApic, session: ApicSession
    ) -> None:
        (item,) = session.get("/api/mo/uni/tn-prod/ap-shop.json", **{"rsp-subtree": "full"})
        (epg,) = children_of(item)
        assert attrs_of(epg)["dn"] == "uni/tn-prod/ap-shop/epg-web"
        assert sorted(class_of(c) for c in children_of(epg)) == ["fvRsBd", "fvRsPathAtt"]

    def test_rsp_subtree_no_embeds_nothing(self, seeded: FakeApic, session: ApicSession) -> None:
        (item,) = session.get("/api/mo/uni/tn-prod.json", **{"rsp-subtree": "no"})
        assert children_of(item) == []

    def test_subtree_full_through_the_builder(self, seeded: FakeApic, aci: Niwaki) -> None:
        (prod,) = aci.query("fvTenant").where(name="prod").subtree_full().fetch()
        bd = next(child for child in prod.children if child.dn == "uni/tn-prod/BD-web")
        assert len(bd.children) == 3

    def test_subtree_where_narrows_the_embedded_children(
        self, seeded: FakeApic, aci: Niwaki
    ) -> None:
        web = (
            aci.query(fvBD)
            .under("uni/tn-prod")
            .where(name="web")
            .include("fvSubnet")
            .subtree_where(ip="10.0.2.1/24")
            .one()
        )
        assert [child.dn for child in web.children] == ["uni/tn-prod/BD-web/subnet-[10.0.2.1/24]"]

    def test_faults_are_embedded_and_required_keeps_the_faulted(
        self, seeded: FakeApic, aci: Niwaki
    ) -> None:
        seeded.seed(
            {
                "faultInst": {
                    "attributes": {
                        "dn": "uni/tn-prod/BD-web/fault-F0467",
                        "code": "F0467",
                        "severity": "major",
                    }
                }
            }
        )
        everything = aci.query(fvBD).under("uni/tn-prod").with_faults().fetch()
        assert [bd.dn for bd in everything] == ["uni/tn-prod/BD-db", "uni/tn-prod/BD-web"]
        web = everything[1]
        assert [child.aci_class for child in web.children] == ["faultInst"]

        faulted = aci.query(fvBD).under("uni/tn-prod").with_faults().only_faulted().fetch()
        assert [bd.dn for bd in faulted] == ["uni/tn-prod/BD-web"]

    def test_health_and_faults_together_are_answered(self, seeded: FakeApic, aci: Niwaki) -> None:
        found = aci.query(fvBD).with_health().with_faults().fetch()
        assert len(found) == 3

    def test_faults_as_a_typed_facet(self, seeded: FakeApic, aci: Niwaki) -> None:
        found = aci.query(fvBD).include_subtree(SubtreeInclude.FAULTS).fetch()
        assert len(found) == 3


class TestProperties:
    def test_naming_only_keeps_dn_and_naming_props(
        self, seeded: FakeApic, session: ApicSession
    ) -> None:
        items = session.get("/api/class/fvBD.json", **{"rsp-prop-include": "naming-only"})
        assert {frozenset(attrs_of(item)) for item in items} == {frozenset({"dn", "name"})}

    def test_config_only_drops_the_operational_halo(
        self, fake: FakeApic, session: ApicSession
    ) -> None:
        fake.seed(
            {
                "fvBD": {
                    "attributes": {
                        "dn": "uni/tn-prod/BD-web",
                        "name": "web",
                        "arpFlood": "yes",
                        "modTs": "2026-10-27T10:00:00.000+00:00",
                        "pcTag": "16386",
                    }
                }
            }
        )
        (item,) = session.get("/api/class/fvBD.json", **{"rsp-prop-include": "config-only"})
        assert set(attrs_of(item)) == {"dn", "name", "arpFlood"}

    def test_naming_only_through_the_builder(self, seeded: FakeApic, aci: Niwaki) -> None:
        found = aci.query(fvBD).under("uni/tn-dev").naming_only().fetch()
        assert [bd.dn for bd in found] == ["uni/tn-dev/BD-web"]
        sent = [r for r in seeded.requests if r.url.path == "/api/mo/uni/tn-dev.json"]
        assert sent[-1].url.params["rsp-prop-include"] == "naming-only"


class TestOrdering:
    def test_order_by_ascending_and_descending(self, seeded: FakeApic, aci: Niwaki) -> None:
        names = [bd.dn for bd in aci.query(fvBD).order_by("name").fetch()]
        assert names[0] == "uni/tn-prod/BD-db"
        descending = [bd.dn for bd in aci.query(fvBD).order_by("dn", desc=True).fetch()]
        assert descending == ["uni/tn-prod/BD-web", "uni/tn-prod/BD-db", "uni/tn-dev/BD-web"]

    def test_order_by_several_keys(self, seeded: FakeApic, aci: Niwaki) -> None:
        found = aci.query(fvBD).order_by("arpFlood").order_by("dn", desc=True).fetch()
        assert [bd.dn for bd in found] == [
            "uni/tn-prod/BD-db",
            "uni/tn-dev/BD-web",
            "uni/tn-prod/BD-web",
        ]


class TestPagination:
    def test_a_page_slices_the_ordered_result_and_reports_the_true_total(
        self, seeded: FakeApic
    ) -> None:
        response = seeded.handle(request("GET", "/api/class/fvSubnet.json?page=1&page-size=2"))
        assert envelope(response)["totalCount"] == "3"
        assert dns_of(imdata(response)) == ["uni/tn-prod/BD-web/subnet-[10.0.2.1/24]"]

    def test_a_page_past_the_end_is_empty_with_the_true_total(self, seeded: FakeApic) -> None:
        response = seeded.handle(request("GET", "/api/class/fvSubnet.json?page=5&page-size=2"))
        assert envelope(response)["totalCount"] == "3"
        assert imdata(response) == []

    def test_an_unpaged_answer_reports_its_size(self, seeded: FakeApic) -> None:
        response = seeded.handle(request("GET", "/api/class/fvBD.json"))
        assert envelope(response)["totalCount"] == "3"

    def test_the_builder_pages_through_everything(self, seeded: FakeApic, aci: Niwaki) -> None:
        found = aci.query("fvSubnet").page_size(1).fetch()
        assert len(found) == 3
        pages = [
            r.url.params.get("page")
            for r in seeded.requests
            if r.url.path == "/api/class/fvSubnet.json"
        ]
        assert pages == ["0", "1", "2"]

    def test_count_reads_the_true_total(self, seeded: FakeApic, aci: Niwaki) -> None:
        assert aci.query("fvSubnet").count() == 3
        assert aci.query("fvSubnet").under("uni/tn-prod/BD-web").count() == 2

    def test_a_leading_slice_caps_the_result(self, seeded: FakeApic, aci: Niwaki) -> None:
        assert [s.dn for s in aci.query("fvSubnet")[:2]] == [
            "uni/tn-prod/BD-db/subnet-[10.0.3.1/24]",
            "uni/tn-prod/BD-web/subnet-[10.0.1.1/24]",
        ]

    def test_first_and_exists(self, seeded: FakeApic, aci: Niwaki) -> None:
        first = aci.query(fvBD).first()
        assert first is not None
        assert first.dn == "uni/tn-dev/BD-web"
        assert aci.query(fvBD).where(name="web").exists()
        assert not aci.query(fvBD).where(name="ghost").exists()
