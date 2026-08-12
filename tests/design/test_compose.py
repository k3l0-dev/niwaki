"""``Cursor.slice`` / ``merge`` — design composition (2.0 it.3 lot C)."""

from __future__ import annotations

import json

import pytest

from niwaki.design import design, merge, ref, tenant
from niwaki.exceptions import DesignError, MergeConflictError
from tests.design.test_import import canonical, find_node


def _two_tenant_design():
    cfg = design()
    prod = cfg.tenant("prod")
    prod.vrf("main")
    prod.bd("web", unicast_routing=True).bind(vrf="main").subnet("10.0.1.1/24")
    prod.contract("web")
    prod.app("site").epg("front").bind(bd="web").provide("web")
    dev = cfg.tenant("dev")
    dev.vrf("lab")
    return cfg


class TestSlice:
    def test_slice_carves_one_tenant_with_bare_ancestors(self) -> None:
        staged = _two_tenant_design().slice("uni/tn-prod")
        classes = [n.aci_class for n in staged.view()]
        assert "fvTenant" in classes
        assert len(staged.view().by_class("fvTenant")) == 1  # dev is gone
        assert staged.view().get("uni/tn-dev") is None
        assert staged.view()["uni/tn-prod/BD-web"].attrs == {"unicast_routing": True}

    def test_slice_equals_the_hand_built_equivalent(self) -> None:
        staged = _two_tenant_design().slice("uni/tn-prod")
        hand = tenant("prod")
        hand.vrf("main")
        hand.bd("web", unicast_routing=True).bind(vrf="main").subnet("10.0.1.1/24")
        hand.contract("web")
        hand.app("site").epg("front").bind(bd="web").provide("web")
        assert canonical(staged.to_payload()) == canonical(hand.to_payload())

    def test_slice_deep_subtree_gets_bare_ancestors(self) -> None:
        staged = _two_tenant_design().slice("uni/tn-prod/BD-web")
        t = find_node(staged.design_node, "fvTenant")
        assert t is not None
        assert t.attrs == {}  # bare Day-2 upsert
        bd = find_node(staged.design_node, "fvBD")
        assert bd is not None
        assert bd.attrs == {"unicast_routing": True}

    def test_out_of_slice_name_target_pins_the_exact_wire(self) -> None:
        # Slicing the BD alone: its vrf bind targets a VRF outside the slice —
        # the wire must still carry tnFvCtxName=main, without the closed world.
        staged = _two_tenant_design().slice("uni/tn-prod/BD-web")
        payload = json.dumps(staged.to_payload())
        assert '"tnFvCtxName": "main"' in payload
        bd = find_node(staged.design_node, "fvBD")
        assert bd is not None
        assert not bd.binds  # converted, not kept as a dangling bind

    def test_out_of_slice_dn_target_becomes_bind_dn(self) -> None:
        cfg = design()
        cfg.infra().vlan_pool("prod", "static")
        cfg.phys_dom("phys").bind(vlan_pool="prod")
        staged = cfg.slice("uni/phys-phys")
        dom = find_node(staged.design_node, "physDomP")
        assert dom is not None
        (bind,) = dom.binds
        assert bind.kind == "bind_dn"
        assert bind.target_name == "uni/infra/vlanns-[prod]-static"
        # And the emitted wire is byte-identical to the unsliced relation.
        assert '"tDn": "uni/infra/vlanns-[prod]-static"' in json.dumps(staged.to_payload())

    def test_inverse_edge_rs_outside_the_slice_is_not_carried(self) -> None:
        cfg = design()
        t = cfg.tenant("prod")
        t.vrf("main").bind(l3out="wan")  # inverse: the Rs lives under the l3out
        t.l3out("wan")
        staged = cfg.slice("uni/tn-prod/ctx-main")
        assert '"l3extRsEctx"' not in json.dumps(staged.to_payload())

    def test_slice_at_uni_copies_the_whole_design(self) -> None:
        cfg = _two_tenant_design()
        assert canonical(cfg.slice("uni").to_payload()) == canonical(cfg.to_payload())

    def test_slice_never_mutates_the_source(self) -> None:
        from niwaki.design import Cursor

        cfg = _two_tenant_design()
        before = canonical(cfg.to_payload())
        staged = cfg.slice("uni/tn-prod")
        Cursor(staged.design_node.children[0]).bd("added-after")
        assert canonical(cfg.to_payload()) == before

    def test_unknown_dn_is_refused(self) -> None:
        with pytest.raises(DesignError, match="nothing at"):
            _two_tenant_design().slice("uni/tn-ghost")


class TestMerge:
    def test_union_of_disjoint_designs(self) -> None:
        a = tenant("prod")
        a.bd("web")
        b = tenant("dev")
        b.vrf("lab")
        combined = merge(a, b)
        view = combined.view()
        assert view.get("uni/tn-prod/BD-web") is not None
        assert view.get("uni/tn-dev/ctx-lab") is not None

    def test_overlay_completes_a_shared_object(self) -> None:
        base = tenant("prod")
        base.bd("web", arp_flooding=False)
        overlay = tenant("prod")
        overlay.bd("web").set(unicast_routing=True).bind(vrf="main")
        overlay.vrf("main")
        combined = merge(base, overlay)
        bd = combined.view()["uni/tn-prod/BD-web"]
        assert bd.attrs == {"arp_flooding": False, "unicast_routing": True}
        assert [b.alias for b in bd.binds] == ["vrf"]

    def test_identical_declarations_collapse(self) -> None:
        a = tenant("prod")
        a.bd("web", unicast_routing=True).bind(vrf="main")
        a.vrf("main")
        b = tenant("prod")
        b.bd("web", unicast_routing=True).bind(vrf="main")
        b.vrf("main")
        combined = merge(a, b)
        assert canonical(combined.to_payload()) == canonical(a.to_payload())

    def test_contradiction_collects_and_raises(self) -> None:
        a = tenant("prod")
        a.bd("web", unicast_routing=True)
        a.bd("db").raw_set(arpFlood="yes")
        b = tenant("prod")
        b.bd("web", unicast_routing=False)
        b.bd("db").raw_set(arpFlood="no")
        with pytest.raises(MergeConflictError) as exc:
            merge(a, b)
        conflicts = exc.value.conflicts
        assert len(conflicts) == 2
        assert {what for _, what, _ in conflicts} == {"unicast_routing", "arpFlood"}

    def test_slice_then_merge_reassembles_the_design(self) -> None:
        cfg = _two_tenant_design()
        reassembled = merge(cfg.slice("uni/tn-prod"), cfg.slice("uni/tn-dev"))
        assert canonical(reassembled.to_payload()) == canonical(cfg.to_payload())

    def test_merge_never_mutates_the_sources(self) -> None:
        a = tenant("prod")
        a.app("ap").epg("e").provide(ref("c", priority="level1"))
        a.contract("c")
        b = tenant("prod")
        from niwaki.design import Cursor

        before_a, before_b = canonical(a.to_payload()), canonical(b.to_payload())
        merged = merge(a, b)
        Cursor(merged.design_node.children[0]).bd("late")
        assert canonical(a.to_payload()) == before_a
        assert canonical(b.to_payload()) == before_b

    def test_fewer_than_two_designs_is_refused(self) -> None:
        with pytest.raises(DesignError, match="at least two"):
            merge(tenant("prod"))


class TestReviewScenariosIt3:
    """The 2026-08-11 adversarial pass on it.3 — every finding pinned."""

    def test_slicing_the_target_side_keeps_the_inverse_rs(self) -> None:
        # B1: vrf.bind(l3out=...) lands its Rs UNDER the l3out — slicing the
        # l3out must keep that attach, pinned with the exact tn name.
        cfg = design()
        t = cfg.tenant("prod")
        t.vrf("main").bind(l3out="edge")
        t.l3out("edge")
        staged = cfg.slice("uni/tn-prod/out-edge")
        payload = json.dumps(staged.to_payload())
        assert '"l3extRsEctx"' in payload
        assert '"tnFvCtxName": "main"' in payload

    def test_merge_wire_spelling_and_coerced_value_agree(self) -> None:
        # M1: an imported design holds wire strings, a hand overlay holds
        # coerced values — one wire value, never a conflict.
        from niwaki.design import from_payload

        hand = tenant("prod")
        hand.bd("web", unicast_routing=True)
        imported = from_payload(hand.to_payload())
        combined = merge(hand, imported)
        assert canonical(combined.to_payload()) == canonical(hand.to_payload())

    def test_merge_sees_cross_channel_contradictions(self) -> None:
        # M2: a typed field and a raw_set of the same property must agree.
        a = tenant("prod")
        a.bd("web", unicast_routing=False)
        b = tenant("prod")
        b.bd("web").raw_set(unicastRoute="yes")
        with pytest.raises(MergeConflictError) as exc:
            merge(a, b)
        ((dn, what, _values),) = exc.value.conflicts
        assert dn == "uni/tn-prod/BD-web"
        assert what == "unicastRoute"

    def test_merge_cross_channel_agreement_is_silent(self) -> None:
        a = tenant("prod")
        a.bd("web", unicast_routing=True)
        b = tenant("prod")
        b.bd("web").raw_set(unicastRoute="yes")
        combined = merge(a, b)  # "yes" and True are one wire value
        bd = combined.view()["uni/tn-prod/BD-web"]
        assert bd.attrs.get("unicast_routing") is True

    def test_merge_handles_set_valued_ref_attrs(self) -> None:
        # M3: Flags ref-attrs are sets — the dedup key must not choke.
        a = tenant("prod")
        a.filter("f").entry("e")
        a.contract("c").subject("s").bind(filter=ref("f", directives={"log"}))
        b = tenant("prod")
        b.contract("c").subject("s").bind(filter=ref("f", directives={"log"}))
        combined = merge(a, b)
        subject = combined.view()["uni/tn-prod/brc-c/subj-s"]
        assert len(subject.binds) == 1  # identical set-attrs collapsed

    def test_merge_conflict_sorting_survives_mixed_types(self) -> None:
        # M4: the error path itself must not TypeError on mixed value types.
        a = tenant("prod")
        a.bd("web", unicast_routing=True)
        b = tenant("prod")
        b.bd("web", unicast_routing=False)
        c = tenant("prod")
        c.bd("web").raw_set(unicastRoute="disabled-nonsense")
        with pytest.raises(MergeConflictError):
            merge(a, b, c)

    def test_slice_collapses_duplicate_identical_binds(self) -> None:
        # M5: a pushable design (duplicate identical binds collapse at
        # resolve time) must slice without a DuplicateDeclarationError.
        cfg = design()
        t = cfg.tenant("prod")
        bd = t.bd("web")
        bd.bind(vrf="main")
        bd.bind(vrf="main")  # legal — the resolver collapses it
        t.vrf("main")
        staged = cfg.slice("uni/tn-prod/BD-web")
        assert json.dumps(staged.to_payload()).count('"tnFvCtxName"') == 1

    def test_scope_tree_incoherence_is_refused(self) -> None:
        # M6: a scope DN lying about the tree it carries must not import.
        from niwaki.design import to_design
        from niwaki.snapshot import Snapshot

        snap = Snapshot(
            scope="uni/tn-x",
            tree={"class": "fvBD", "rn": "BD-web", "attributes": {"name": "web"}, "children": []},
        )
        with pytest.raises(DesignError, match="inconsistent"):
            to_design(snap)

    def test_nested_poluni_in_a_payload_is_refused(self) -> None:
        # M7: the config universe is declared exactly once.
        from niwaki.design import from_payload

        payload = {
            "polUni": {
                "attributes": {},
                "children": [{"polUni": {"attributes": {}, "children": []}}],
            }
        }
        with pytest.raises(DesignError, match="nested"):
            from_payload(payload)

    def test_slice_shares_no_mutable_value_with_the_source(self) -> None:
        # M8: mutating a Flags set inside the slice must not touch the source.
        from niwaki.models._generated.enums.L4TcpFlags import L4TcpFlags

        cfg = design()
        t = cfg.tenant("prod")
        t.filter("f").entry("e", tcp_rules={L4TcpFlags("syn")})
        before = canonical(cfg.to_payload())
        staged = cfg.slice("uni/tn-prod")
        entry = find_node(staged.design_node, "vzEntry")
        assert entry is not None
        entry.attrs["tcp_rules"].add(L4TcpFlags("ack"))
        assert canonical(cfg.to_payload()) == before

    def test_view_shares_no_mutable_value_with_the_design(self) -> None:
        from niwaki.models._generated.enums.L4TcpFlags import L4TcpFlags

        cfg = tenant("prod")
        cfg.filter("f").entry("e", tcp_rules={L4TcpFlags("syn")})
        before = canonical(cfg.to_payload())
        view = cfg.view()
        view["uni/tn-prod/flt-f/e-e"].attrs["tcp_rules"].add(L4TcpFlags("ack"))
        assert canonical(cfg.to_payload()) == before

    def test_composition_results_carry_the_typed_root_cursor(self) -> None:
        # m5: slice/merge results expose the same typed surface as design().
        combined = merge(tenant("a"), tenant("b"))
        staged = combined.slice("uni/tn-a")
        assert type(combined).__name__ == "UniCursor"
        assert type(staged).__name__ == "UniCursor"
