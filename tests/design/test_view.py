"""``DesignView`` — the frozen, walkable projection of a design (2.0 it.3 lot A)."""

from __future__ import annotations

import pytest

from niwaki.design import DesignView, design, ref, tenant


def _rich_design():
    cfg = design()
    t = cfg.tenant("prod")
    t.vrf("main")
    bd = t.bd("web").set(unicast_routing=True)
    bd.bind(vrf="main")
    bd.subnet("10.0.1.1/24")
    t.contract("web")
    epg = t.app("site").epg("front")
    epg.bind(bd="web")
    epg.provide(ref("web", priority="level1"))
    epg.bind_dn(domain="uni/phys-ghost")
    bd.raw_set(arpFlood="yes")
    cfg.controller().raw("infraKafkaPol", name="kafkapol", mode="OFF")
    return cfg


class TestDesignView:
    def test_walk_is_parents_first_declaration_order(self) -> None:
        view = _rich_design().view()
        classes = [node.aci_class for node in view]
        assert classes[0] == "polUni"
        assert classes.index("fvTenant") < classes.index("fvBD")
        assert classes.index("fvBD") < classes.index("fvSubnet")
        assert classes.index("fvAp") < classes.index("fvAEPg")

    def test_lookup_by_dn_and_class(self) -> None:
        view = _rich_design().view()
        bd = view["uni/tn-prod/BD-web"]
        assert bd.aci_class == "fvBD"
        assert bd.label == "bd"
        assert bd.position == "tenant.bd"
        assert bd.naming == {"name": "web"}
        assert bd.attrs == {"unicast_routing": True}
        assert view.get("uni/tn-prod/BD-nope") is None
        with pytest.raises(KeyError):
            view["uni/tn-prod/BD-nope"]
        assert [n.dn for n in view.by_class("fvCtx")] == ["uni/tn-prod/ctx-main"]
        assert "uni/tn-prod" in view
        assert len(view) == len(list(view))

    def test_binds_project_with_their_configuration(self) -> None:
        view = _rich_design().view()
        epg = view["uni/tn-prod/ap-site/epg-front"]
        by_alias = {b.alias: b for b in epg.binds}
        assert by_alias["bd"].kind == "bind"
        assert by_alias["bd"].target == "web"
        assert by_alias["provide"].kind == "verb"
        assert by_alias["provide"].attrs == {"priority": "level1"}
        assert by_alias["domain"].kind == "bind_dn"
        assert by_alias["domain"].target == "uni/phys-ghost"

    def test_raw_surfaces_project_wire_spelled(self) -> None:
        view = _rich_design().view()
        assert view["uni/tn-prod/BD-web"].raw_attrs == {"arpFlood": "yes"}
        kafka = view["uni/controller/kafkapol"]
        assert kafka.aci_class == "infraKafkaPol"

    def test_view_is_a_snapshot_not_a_live_facade(self) -> None:
        cfg = tenant("prod")
        view = cfg.view()
        cfg.bd("late")
        assert view.get("uni/tn-prod/BD-late") is None
        assert cfg.view().get("uni/tn-prod/BD-late") is not None

    def test_mutating_the_view_does_not_touch_the_design(self) -> None:
        cfg = tenant("prod")
        cfg.bd("web").set(unicast_routing=True)
        view = cfg.view()
        view["uni/tn-prod/BD-web"].attrs["unicast_routing"] = False
        assert '"unicastRoute": "true"' in str(cfg.to_payload()).replace("'", '"')

    def test_view_from_any_cursor_covers_the_whole_design(self) -> None:
        cfg = design()
        deep = cfg.tenant("prod").bd("web")
        view = deep.view()  # called from a leaf cursor
        assert view.root.aci_class == "polUni"
        assert isinstance(view, DesignView)

    def test_empty_design_views_as_its_root_alone(self) -> None:
        view = design().view()
        assert len(view) == 1
        assert view.root.dn == "uni"
        assert view.root.children == ()
