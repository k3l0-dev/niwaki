"""``from_payload`` + scoped ``to_design`` — the slice at import (2.0 it.3 lot B)."""

from __future__ import annotations

import json

import pytest

from niwaki.design import design, from_payload, tenant, to_design
from niwaki.exceptions import DesignError, SnapshotImportError
from niwaki.snapshot import Snapshot
from tests.design.test_import import canonical, find_node, payload_to_tree


def _tenant_tree() -> dict:
    """A tenant subtree as a scoped capture would carry it (root = the tenant)."""
    return {
        "class": "fvTenant",
        "rn": "tn-prod",
        "attributes": {"name": "prod"},
        "children": [
            {
                "class": "fvBD",
                "rn": "BD-web",
                "attributes": {"name": "web", "unicastRoute": "yes"},
                "children": [
                    {
                        "class": "fvRsCtx",
                        "rn": "rsctx",
                        "attributes": {"tnFvCtxName": "main"},
                        "children": [],
                    }
                ],
            },
            {"class": "fvCtx", "rn": "ctx-main", "attributes": {"name": "main"}, "children": []},
        ],
    }


class TestScopedImport:
    def test_tenant_scope_imports_under_a_bare_chain(self) -> None:
        snap = Snapshot(scope="uni/tn-prod", tree=_tenant_tree())
        cfg = to_design(snap)
        t = find_node(cfg.design_node, "fvTenant")
        assert t is not None
        assert t.attrs == {"name": "prod"} or t.naming == {"name": "prod"}
        bd = find_node(cfg.design_node, "fvBD")
        assert bd is not None
        assert [b.alias for b in bd.binds] == ["vrf"]  # closed-world within the slice

    def test_deep_scope_rebuilds_every_ancestor_bare(self) -> None:
        bd_tree = _tenant_tree()["children"][0]
        bd_tree["children"] = []  # the rsctx target is outside this capture
        snap = Snapshot(scope="uni/tn-prod/BD-web", tree=bd_tree)
        cfg = to_design(snap)
        t = find_node(cfg.design_node, "fvTenant")
        assert t is not None
        assert t.attrs == {}  # bare Day-2 upsert — no attribute touched
        bd = find_node(cfg.design_node, "fvBD")
        assert bd is not None
        assert bd.attrs.get("unicast_routing") == "yes"
        # The typed route normalises the wire synonym ("yes" → "true").
        payload = json.dumps(cfg.to_payload())
        assert '"unicastRoute": "true"' in payload

    def test_scoped_payload_matches_a_hand_built_design(self) -> None:
        snap = Snapshot(scope="uni/tn-prod", tree=_tenant_tree())
        imported = to_design(snap)
        hand = tenant("prod")
        hand.bd("web", unicast_routing=True).bind(vrf="main")
        hand.vrf("main")
        assert canonical(imported.to_payload()) == canonical(hand.to_payload())

    def test_dangling_name_relation_in_a_slice_falls_to_raw(self) -> None:
        # The BD binds a VRF that the slice does not carry: the closed world
        # cannot resolve it, the wire still must — raw Rs, exact tn prop.
        tree = _tenant_tree()
        tree["children"] = [tree["children"][0]]  # drop the fvCtx
        snap = Snapshot(scope="uni/tn-prod", tree=tree)
        cfg = to_design(snap)
        bd = find_node(cfg.design_node, "fvBD")
        assert bd is not None
        assert not bd.binds
        assert [c.aci_class for c in bd.children] == ["fvRsCtx"]
        assert '"tnFvCtxName": "main"' in json.dumps(cfg.to_payload())

    def test_scope_outside_uni_is_refused(self) -> None:
        snap = Snapshot(
            scope="topology/pod-1",
            tree={"class": "fabricPod", "rn": "pod-1", "attributes": {}, "children": []},
        )
        with pytest.raises(DesignError, match="outside"):
            to_design(snap)

    def test_unresolvable_ancestor_segment_is_refused(self) -> None:
        snap = Snapshot(
            scope="uni/nonsense-x/BD-web",
            tree={"class": "fvBD", "rn": "BD-web", "attributes": {"name": "web"}, "children": []},
        )
        with pytest.raises(DesignError, match="no child class"):
            to_design(snap)

    def test_policies_apply_in_scoped_imports_too(self) -> None:
        tree = _tenant_tree()
        tree["children"][0]["attributes"]["brandNewProp"] = "x"
        snap = Snapshot(scope="uni/tn-prod", tree=tree)
        with pytest.raises(SnapshotImportError):
            to_design(snap)
        cfg = to_design(snap, on_unknown="raw")
        bd = find_node(cfg.design_node, "fvBD")
        assert bd is not None
        assert bd.raw_attrs == {"brandNewProp": "x"}


class TestFromPayload:
    def test_round_trips_its_own_emitter(self) -> None:
        original = design()
        t = original.tenant("prod")
        t.vrf("main")
        t.bd("web", unicast_routing=True).bind(vrf="main").subnet("10.0.1.1/24")
        t.filter("http").entry("e", tcp=80)
        t.contract("web").subject("s").bind(filter="http")
        clone = from_payload(original.to_payload())
        assert canonical(clone.to_payload()) == canonical(original.to_payload())

    def test_agrees_with_the_snapshot_door(self) -> None:
        original = tenant("prod")
        original.bd("web")
        payload = original.to_payload()
        via_payload = from_payload(payload)
        via_snapshot = to_design(Snapshot(scope="uni", tree=payload_to_tree(payload)))
        assert canonical(via_payload.to_payload()) == canonical(via_snapshot.to_payload())

    def test_non_poluni_root_is_refused(self) -> None:
        with pytest.raises(DesignError, match="polUni"):
            from_payload({"fvTenant": {"attributes": {"name": "x"}}})

    def test_missing_naming_value_is_refused(self) -> None:
        payload = {
            "polUni": {
                "attributes": {},
                "children": [{"fvTenant": {"attributes": {"descr": "no name"}}}],
            }
        }
        with pytest.raises(DesignError, match="naming"):
            from_payload(payload)

    def test_status_directive_is_refused(self) -> None:
        payload = {
            "polUni": {
                "attributes": {},
                "children": [{"fvTenant": {"attributes": {"name": "x", "status": "deleted"}}}],
            }
        }
        with pytest.raises(DesignError, match="status"):
            from_payload(payload)

    def test_dn_and_rn_bookkeeping_keys_are_recomputed_not_imported(self) -> None:
        payload = {
            "polUni": {
                "attributes": {},
                "children": [
                    {
                        "fvTenant": {
                            "attributes": {
                                "name": "x",
                                "dn": "uni/tn-LIES",
                                "rn": "tn-LIES",
                            }
                        }
                    }
                ],
            }
        }
        cfg = from_payload(payload)
        t = find_node(cfg.design_node, "fvTenant")
        assert t is not None
        assert t.rn == "tn-x"  # identity from the naming value, never from dn/rn keys

    def test_unknown_class_in_payload_is_refused(self) -> None:
        payload = {
            "polUni": {
                "attributes": {},
                "children": [{"fvFutureThing": {"attributes": {"name": "x"}}}],
            }
        }
        with pytest.raises(DesignError, match="unknown ACI class"):
            from_payload(payload)
