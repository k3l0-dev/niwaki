"""``to_design()`` — reverse import of a snapshot into the design DSL.

2.0 it.2 lot B.  The importer prefers the curated vocabulary (verbs, makers,
binds) and falls back to the wire-name escape hatches; fidelity is the
invariant, idiomaticity best-effort.  Policies under test, all decided at the
2026-08-09 scoping: unknown class/prop → collect-all raise
(``on_unknown="raw"`` opt-in), redacted secrets → collect-all raise
(``redacted="skip"`` opt-in), unknown enum member → silent auto-escape to the
wire channel (the value is right, the annotation is stale).
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from niwaki.design import design, ref, to_design
from niwaki.design._import import _maker_inversion, _verb_inversion
from niwaki.design._node import DesignNode
from niwaki.exceptions import DesignError, SnapshotImportError
from niwaki.query._catalog import catalog
from niwaki.snapshot import REDACTED, Snapshot

# ── Helpers: payload → pseudo-snapshot, canonical payload comparison ──────────


def payload_to_tree(envelope: dict[str, Any]) -> dict[str, Any]:
    """One payload envelope → the snapshot node shape (wire attrs, sorted)."""
    (cls,) = envelope.keys()
    inner = envelope[cls]
    attrs = dict(inner.get("attributes", {}))
    rn = catalog().rn_format(cls)
    for prop in catalog().class_meta(cls).naming:
        rn = rn.replace(f"{{{prop}}}", attrs.get(prop, ""))
    node: dict[str, Any] = {
        "class": cls,
        "rn": "uni" if cls == "polUni" else rn,
        "attributes": dict(sorted(attrs.items())),
        "children": [payload_to_tree(child) for child in inner.get("children", [])],
    }
    node["children"].sort(key=lambda child: (child["class"], child["rn"]))
    return node


def pseudo_snapshot(payload: dict[str, Any]) -> Snapshot:
    """A snapshot document standing in for ``snapshot.take`` — same shape."""
    return Snapshot(scope="uni", tree=payload_to_tree(payload))


def canonical(envelope: dict[str, Any]) -> str:
    """Order-independent serialisation: child order is a declaration artefact."""

    def norm(env: dict[str, Any]) -> dict[str, Any]:
        (cls,) = env.keys()
        inner = env[cls]
        out: dict[str, Any] = {cls: {"attributes": dict(inner.get("attributes", {}))}}
        children = [norm(child) for child in inner.get("children", [])]
        if children:
            children.sort(key=lambda item: json.dumps(item, sort_keys=True))
            out[cls]["children"] = children
        return out

    return json.dumps(norm(envelope), indent=1, sort_keys=True)


def round_trip(cfg: Any) -> tuple[str, str]:
    """(canonical original payload, canonical re-imported payload)."""
    payload = cfg.to_payload()
    imported = to_design(pseudo_snapshot(payload))
    return canonical(payload), canonical(imported.to_payload())


def find_node(root: DesignNode, aci_class: str) -> DesignNode | None:
    for node in root.iter_subtree():
        if node.aci_class == aci_class:
            return node
    return None


def _tenant_snapshot(**bd_extra: str) -> Snapshot:
    """A minimal hand-built uni snapshot: tenant → (vrf, bd→bind(vrf))."""
    bd_attrs = {"name": "web", **bd_extra}
    tree = {
        "class": "polUni",
        "rn": "uni",
        "attributes": {},
        "children": [
            {
                "class": "fvTenant",
                "rn": "tn-prod",
                "attributes": {"name": "prod"},
                "children": [
                    {
                        "class": "fvBD",
                        "rn": "BD-web",
                        "attributes": dict(sorted(bd_attrs.items())),
                        "children": [
                            {
                                "class": "fvRsCtx",
                                "rn": "rsctx",
                                "attributes": {"tnFvCtxName": "main"},
                                "children": [],
                            }
                        ],
                    },
                    {
                        "class": "fvCtx",
                        "rn": "ctx-main",
                        "attributes": {"name": "main"},
                        "children": [],
                    },
                ],
            }
        ],
    }
    return Snapshot(scope="uni", tree=tree)


# ── Round trips (payload → pseudo-snapshot → to_design → same payload) ────────


class TestRoundTrip:
    def test_tenant_world(self) -> None:
        cfg = design()
        t = cfg.tenant("prod")
        t.vrf("main")
        bd = t.bd("web").set(unicast_routing=True, arp_flooding=False)
        bd.bind(vrf="main")
        bd.subnet("10.0.1.1/24", scope="public,shared")
        t.filter("http").entry("tcp-80", tcp=80)
        t.contract("web").subject("s1").bind(filter=ref("http", directives={"log"}))
        epg = t.app("site").epg("front")
        epg.bind(bd="web")
        epg.provide("web")
        epg.consume("web")
        original, reimported = round_trip(cfg)
        assert original == reimported

    def test_access_and_l3out_world(self) -> None:
        cfg = design()
        infra = cfg.infra()
        infra.vlan_pool("prod", "static").range("vlan-2600", "vlan-2699")
        cfg.l3_dom("l3dom").bind(vlan_pool="prod")
        cfg.phys_dom("phys").bind(vlan_pool="prod")
        infra.aaep("aaep").bind(domain="phys")
        t = cfg.tenant("prod")
        t.vrf("main")
        out = t.l3out("wan")
        out.bind(vrf="main").bind(domain="l3dom")
        np = out.node_profile("np-1", dscp_value="AF11")
        att = np.node_attachment("topology/pod-1/node-101", router_id="10.0.0.1")
        att.loopback(loop_back_interface_address="10.0.0.2")
        t.bd("web").bind(vrf="main")
        epg = t.app("site").epg("front").bind(bd="web")
        epg.static_path(
            "topology/pod-1/paths-101/pathep-[eth1/1]",
            encap="vlan-2601",
            deployment_immediacy="immediate",
        )
        epg.bind(domain=ref("phys", resolution_immediacy="immediate"))
        original, reimported = round_trip(cfg)
        assert original == reimported

    def test_vzany_and_fabric(self) -> None:
        cfg = design()
        cfg.fabric().datetime_policy("ntp")
        t = cfg.tenant("prod")
        t.contract("any-c")
        vzany = t.vrf("main").vzany()
        vzany.provide("any-c")
        vzany.consume("any-c")
        original, reimported = round_trip(cfg)
        assert original == reimported

    def test_bind_dn_when_target_outside_snapshot(self) -> None:
        cfg = design()
        cfg.l3_dom("l3dom").bind_dn(vlan_pool="uni/infra/vlanns-[ghost]-static")
        original, reimported = round_trip(cfg)
        assert original == reimported

    def test_raw_set_known_prop_normalises_through_the_typed_route(self) -> None:
        # A known property that arrived via raw_set() re-imports through the
        # typed route, so a synonym wire spelling normalises ("yes" → "true").
        # Wire-equivalent by the APIC's own grammar — the coerced comparison
        # of mo_diff (lot A) is the proof — and the typed design is the more
        # faithful expression, so the normalisation is intended.
        cfg = design()
        cfg.tenant("prod").bd("web").raw_set(arpFlood="yes")
        imported = to_design(pseudo_snapshot(cfg.to_payload()))
        bd = find_node(imported.design_node, "fvBD")
        assert bd is not None
        assert not bd.raw_attrs  # typed field, not a wire escape any more
        assert '"arpFlood": "true"' in json.dumps(imported.to_payload())


# ── Idiomaticity: the importer prefers the curated vocabulary ─────────────────


class TestCuratedInversion:
    def test_relationship_becomes_a_bind(self) -> None:
        cfg = to_design(_tenant_snapshot())
        bd = find_node(cfg.design_node, "fvBD")
        assert bd is not None
        assert [b.kind for b in bd.binds] == ["bind"]
        assert bd.binds[0].alias == "vrf"
        assert bd.binds[0].target_name == "main"
        assert not bd.children  # the fvRsCtx is a bind, not a structural child

    def test_contract_relationship_becomes_a_verb(self) -> None:
        cfg = design()
        t = cfg.tenant("prod")
        t.contract("web")
        t.app("a").epg("e").provide("web")
        imported = to_design(pseudo_snapshot(cfg.to_payload()))
        epg = find_node(imported.design_node, "fvAEPg")
        assert epg is not None
        assert [(b.kind, b.alias) for b in epg.binds] == [("verb", "provide")]

    def test_maker_position_is_recovered(self) -> None:
        cfg = design()
        cfg.tenant("prod").bd("web")
        imported = to_design(pseudo_snapshot(cfg.to_payload()))
        bd = find_node(imported.design_node, "fvBD")
        assert bd is not None
        assert bd.label == "bd"
        assert bd.position == "tenant.bd"

    def test_dangling_name_relation_falls_back_to_raw(self) -> None:
        snap = _tenant_snapshot()
        assert snap.tree is not None
        rs = snap.tree["children"][0]["children"][0]["children"][0]
        rs["attributes"]["tnFvCtxName"] = "ghost"  # no such VRF anywhere
        cfg = to_design(snap)
        bd = find_node(cfg.design_node, "fvBD")
        assert bd is not None
        assert not bd.binds  # bind() would fail closed-world at push
        assert [child.aci_class for child in bd.children] == ["fvRsCtx"]
        payload = cfg.to_payload()
        assert '"tnFvCtxName": "ghost"' in json.dumps(payload, indent=0)

    def test_relationship_with_children_is_not_bound(self) -> None:
        # l3extRsNodeL3OutAtt carries children (loopbacks) — it must come back
        # as its maker (node_attachment), never as a childless bind.
        cfg = design()
        np = cfg.tenant("p").l3out("o").node_profile("np")
        np.node_attachment("topology/pod-1/node-101", router_id="1.1.1.1").loopback(
            loop_back_interface_address="2.2.2.2"
        )
        imported = to_design(pseudo_snapshot(cfg.to_payload()))
        att = find_node(imported.design_node, "l3extRsNodeL3OutAtt")
        assert att is not None
        assert att.label == "node_attachment"
        assert [child.aci_class for child in att.children] == ["l3extLoopBackIfP"]


# ── Policy: unknown class / unknown property ──────────────────────────────────


class TestUnknownPolicy:
    def _snapshot_with_unknown_class(self) -> Snapshot:
        snap = _tenant_snapshot()
        assert snap.tree is not None
        tenant_node = snap.tree["children"][0]
        tenant_node["children"].append(
            {
                "class": "fvFutureThing",
                "rn": "future-x",
                "attributes": {"name": "x", "shiny": "yes"},
                "children": [],
            }
        )
        return snap

    def test_unknown_class_raises_collect_all(self) -> None:
        snap = self._snapshot_with_unknown_class()
        assert snap.tree is not None
        snap.tree["children"][0]["children"].append(
            {"class": "fvOtherThing", "rn": "other-y", "attributes": {}, "children": []}
        )
        with pytest.raises(SnapshotImportError) as exc:
            to_design(snap)
        problems = exc.value.problems
        assert [p.kind for p in problems] == ["unknown-class", "unknown-class"]
        assert {p.dn for p in problems} == {
            "uni/tn-prod/future-x",
            "uni/tn-prod/other-y",
        }

    def test_unknown_class_raw_opt_in_carries_verbatim(self) -> None:
        cfg = to_design(self._snapshot_with_unknown_class(), on_unknown="raw")
        node = find_node(cfg.design_node, "fvFutureThing")
        assert node is not None
        assert node.rn == "future-x"
        payload = json.dumps(cfg.to_payload())
        assert '"fvFutureThing"' in payload
        assert '"shiny": "yes"' in payload

    def test_unknown_property_raises_collect_all(self) -> None:
        snap = _tenant_snapshot(brandNewProp="x")
        with pytest.raises(SnapshotImportError) as exc:
            to_design(snap)
        (problem,) = exc.value.problems
        assert problem.kind == "unknown-property"
        assert problem.dn == "uni/tn-prod/BD-web"
        assert "brandNewProp" in problem.detail

    def test_unknown_property_raw_opt_in_rides_the_wire_channel(self) -> None:
        cfg = to_design(_tenant_snapshot(brandNewProp="x"), on_unknown="raw")
        bd = find_node(cfg.design_node, "fvBD")
        assert bd is not None
        assert bd.raw_attrs == {"brandNewProp": "x"}
        assert '"brandNewProp": "x"' in json.dumps(cfg.to_payload())

    def test_containment_the_tables_lack_is_trusted(self) -> None:
        # A catalogue-known class in a position CHILD_MAP does not list: the
        # fabric is the authority on its own edges, so the snapshot's
        # placement imports as-is — no problem, no opt-in (measured live:
        # commTelnet under commPol, uiSettingsCont under polUni).
        snap = _tenant_snapshot()
        assert snap.tree is not None
        snap.tree["children"][0]["children"].append(
            {"class": "topSystem", "rn": "sys", "attributes": {}, "children": []}
        )
        cfg = to_design(snap)
        assert '"topSystem"' in json.dumps(cfg.to_payload())

    def test_generated_class_in_untabled_position_is_trusted(self) -> None:
        # Same trust rule through the typed branch: a generated class whose
        # containment the tables lack still imports, fully typed.
        snap = _tenant_snapshot()
        assert snap.tree is not None
        snap.tree["children"].append(
            {
                "class": "fvSubnet",
                "rn": "subnet-[10.8.8.1/24]",
                "attributes": {"ip": "10.8.8.1/24"},
                "children": [],
            }
        )
        cfg = to_design(snap)
        sn = find_node(cfg.design_node, "fvSubnet")
        assert sn is not None
        assert sn.cls.__name__ == "fvSubnet"  # typed node, not a raw one


# ── Policy: redacted secrets ──────────────────────────────────────────────────


class TestRedactedPolicy:
    def test_redacted_value_raises_collect_all(self) -> None:
        snap = _tenant_snapshot(descr=REDACTED)
        with pytest.raises(SnapshotImportError) as exc:
            to_design(snap)
        (problem,) = exc.value.problems
        assert problem.kind == "redacted-value"
        assert problem.dn == "uni/tn-prod/BD-web"
        assert "descr" in problem.detail

    def test_redacted_skip_drops_the_value_only(self) -> None:
        cfg = to_design(_tenant_snapshot(descr=REDACTED), redacted="skip")
        bd = find_node(cfg.design_node, "fvBD")
        assert bd is not None
        assert "description" not in bd.attrs
        assert REDACTED not in json.dumps(cfg.to_payload())
        # the bind on the same node still imported
        assert [b.alias for b in bd.binds] == ["vrf"]


# ── Policy: unknown enum member auto-escapes ──────────────────────────────────


class TestEnumEscape:
    def test_unknown_enum_member_escapes_to_the_wire_channel(self) -> None:
        snap = _tenant_snapshot()
        assert snap.tree is not None
        vrf = snap.tree["children"][0]["children"][1]
        vrf["attributes"]["ipDataPlaneLearning"] = "futuremode"
        cfg = to_design(snap)  # no opt-in needed
        ctx = find_node(cfg.design_node, "fvCtx")
        assert ctx is not None
        assert ctx.raw_attrs == {"ipDataPlaneLearning": "futuremode"}
        assert "data_plane_learning" not in ctx.attrs
        assert '"ipDataPlaneLearning": "futuremode"' in json.dumps(cfg.to_payload())

    def test_known_enum_member_stays_typed(self) -> None:
        snap = _tenant_snapshot()
        assert snap.tree is not None
        vrf = snap.tree["children"][0]["children"][1]
        vrf["attributes"]["ipDataPlaneLearning"] = "disabled"
        cfg = to_design(snap)
        ctx = find_node(cfg.design_node, "fvCtx")
        assert ctx is not None
        assert not ctx.raw_attrs
        assert "data_plane_learning" in ctx.attrs

    def test_refused_non_default_value_escapes_to_the_wire(self) -> None:
        # Any model-refused value that is NOT the schema default rides the
        # wire channel verbatim: the APIC served it, the wire is
        # authoritative, and the controller stays the only judge at push.
        snap = _tenant_snapshot(descr="x" * 300)  # exceeds the schema bound
        cfg = to_design(snap)
        bd = find_node(cfg.design_node, "fvBD")
        assert bd is not None
        assert bd.raw_attrs == {"descr": "x" * 300}

    def test_named_number_out_of_bounds_escapes_to_the_wire(self) -> None:
        # int | Literal[...] union: an out-of-range number fails both
        # branches; not the default → wire channel, no problem.
        cfg = design()
        t = cfg.tenant("prod")
        t.filter("f").entry("e")
        payload = cfg.to_payload()
        snap = pseudo_snapshot(payload)
        assert snap.tree is not None
        entry = snap.tree["children"][0]["children"][0]["children"][0]
        assert entry["class"] == "vzEntry"
        entry["attributes"]["dFromPort"] = "99999"
        imported = to_design(snap)
        node = find_node(imported.design_node, "vzEntry")
        assert node is not None
        assert node.raw_attrs == {"dFromPort": "99999"}

    def test_naming_value_the_model_refuses_is_collected(self) -> None:
        # Identity has no wire-channel escape: a naming value the model
        # refuses is the one value family that still collects.
        snap = _tenant_snapshot()
        assert snap.tree is not None
        tenant_node = snap.tree["children"][0]
        tenant_node["rn"] = "tn-a b"
        tenant_node["attributes"]["name"] = "a b"  # space: refused by pattern
        with pytest.raises(SnapshotImportError) as exc:
            to_design(snap)
        assert any(p.kind == "invalid-value" for p in exc.value.problems)


# ── The real-fabric halo (what a live snapshot actually carries) ──────────────


#: The universal wire halo: every object of a live snapshot carries its
#: configurable properties at their current values — unset spellings included.
_HALO = {"annotation": "", "nameAlias": "", "ownerKey": "", "ownerTag": ""}


class TestLiveWireHalo:
    """A snapshot is wire truth, defaults and unset markers included.

    The gate the first Gate A missed: pseudo-snapshots built from payloads
    only carry explicitly-set fields, but ``snapshot.take`` keeps every
    configurable property — ``""`` on almost every object,
    ``vmac="not-applicable"`` on every BD, ``vrfIndex="0"`` on every VRF.
    """

    def _decorated(self, cfg: Any) -> Snapshot:
        """Pseudo-snapshot of *cfg* with the live halo grafted onto every node."""
        snap = pseudo_snapshot(cfg.to_payload())

        def _walk(node: dict[str, Any]) -> None:
            node["attributes"] = {**_HALO, **node["attributes"]}
            if node["class"] == "fvBD":
                node["attributes"].setdefault("vmac", "not-applicable")
            if node["class"] == "fvCtx":
                node["attributes"].setdefault("vrfIndex", "0")
            for child in node["children"]:
                _walk(child)

        assert snap.tree is not None
        _walk(snap.tree)
        return snap

    def test_halo_imports_clean_and_drops_to_the_original_payload(self) -> None:
        cfg = design()
        t = cfg.tenant("prod")
        t.vrf("main")
        t.bd("web").bind(vrf="main").subnet("10.0.1.1/24")
        t.app("site").epg("front").bind(bd="web").provide("web")
        t.contract("web")
        original = canonical(cfg.to_payload())
        imported = to_design(self._decorated(cfg))  # default policies — no raise
        assert canonical(imported.to_payload()) == original

    def test_empty_string_is_dropped_not_declared(self) -> None:
        # An empty design decorated with the halo: only polUni carries "".
        cfg = to_design(self._decorated(design()))
        assert cfg.design_node.attrs == {}
        assert not cfg.design_node.raw_attrs

    def test_sentinel_default_is_dropped_not_escaped(self) -> None:
        cfg = design()
        cfg.tenant("prod").bd("web")
        imported = to_design(self._decorated(cfg))
        bd = find_node(imported.design_node, "fvBD")
        assert bd is not None
        assert not bd.raw_attrs  # "not-applicable" dropped, never escaped
        assert "virtual_mac_address" not in bd.attrs

    def test_relationship_with_halo_still_becomes_a_bind(self) -> None:
        # The regression that broke every idiomatic inversion: an Rs whose
        # attributes carry the "" halo must still invert to bind().
        snap = _tenant_snapshot()
        assert snap.tree is not None
        rs = snap.tree["children"][0]["children"][0]["children"][0]
        rs["attributes"].update(_HALO)
        cfg = to_design(snap)
        bd = find_node(cfg.design_node, "fvBD")
        assert bd is not None
        assert [b.alias for b in bd.binds] == ["vrf"]
        assert not bd.children

    def test_real_apic_fixture_imports_clean(self) -> None:
        """A real fvBD read (fixture), normalised by the real snapshot code."""
        import json
        from pathlib import Path

        from niwaki.snapshot import _normalise_attrs

        raw = json.loads(Path("tests/fixtures/fvBD_list.json").read_text())
        bd_attrs = dict(raw["imdata"][0]["fvBD"]["attributes"])
        bd_attrs.pop("dn", None)
        rn = bd_attrs.pop("rn", "BD-Prod-BD")
        attrs = _normalise_attrs(catalog(), "fvBD", bd_attrs)
        snap = Snapshot(
            scope="uni",
            tree={
                "class": "polUni",
                "rn": "uni",
                "attributes": {},
                "children": [
                    {
                        "class": "fvTenant",
                        "rn": "tn-Prod",
                        "attributes": {"name": "Prod"},
                        "children": [
                            {"class": "fvBD", "rn": rn, "attributes": attrs, "children": []}
                        ],
                    }
                ],
            },
        )
        cfg = to_design(snap)  # default policies: a real read must import clean
        bd = find_node(cfg.design_node, "fvBD")
        assert bd is not None
        # Typed fields carry the real values (spot checks on the fixture).
        assert bd.attrs.get("arp_flooding") == "no"
        assert bd.attrs.get("unicast_routing") in ("yes", True)


# ── Review scenarios (2026-08-11 adversarial pass) ────────────────────────────


class TestReviewScenarios:
    def test_generated_child_under_unknown_parent_follows_the_policy(self) -> None:
        # A generated class under an unknown-class parent: default collects,
        # on_unknown="raw" carries the whole subtree verbatim.
        snap = _tenant_snapshot()
        assert snap.tree is not None
        snap.tree["children"][0]["children"].append(
            {
                "class": "fvFutureContainer",
                "rn": "future-x",
                "attributes": {},
                "children": [
                    {
                        "class": "fvSubnet",
                        "rn": "subnet-[10.9.9.1/24]",
                        "attributes": {"ip": "10.9.9.1/24"},
                        "children": [],
                    }
                ],
            }
        )
        with pytest.raises(SnapshotImportError) as exc:
            to_design(snap)
        # One problem at the unknown subtree's root — the children are not
        # visited (they are covered by the parent's problem, not noise).
        (problem,) = exc.value.problems
        assert problem.kind == "unknown-class"
        assert "subtree" in problem.detail
        cfg = to_design(snap, on_unknown="raw")
        text = json.dumps(cfg.to_payload())
        assert '"fvFutureContainer"' in text
        assert '"fvSubnet"' in text
        assert '"10.9.9.1/24"' in text

    def test_redacted_reports_exactly_once_when_the_rs_falls_to_raw(self) -> None:
        # A dangling Rs with a redacted attribute crosses two routes (bind →
        # raw); the screen runs once per node, so exactly one problem.
        snap = _tenant_snapshot()
        assert snap.tree is not None
        rs = snap.tree["children"][0]["children"][0]["children"][0]
        rs["attributes"]["tnFvCtxName"] = "ghost"
        rs["attributes"]["descr"] = REDACTED
        with pytest.raises(SnapshotImportError) as exc:
            to_design(snap)
        redacted_problems = [p for p in exc.value.problems if p.kind == "redacted-value"]
        assert len(redacted_problems) == 1

    def test_combo_on_unknown_raw_and_redacted_skip(self) -> None:
        snap = _tenant_snapshot(brandNewProp="x", descr=REDACTED)
        cfg = to_design(snap, on_unknown="raw", redacted="skip")
        bd = find_node(cfg.design_node, "fvBD")
        assert bd is not None
        assert bd.raw_attrs == {"brandNewProp": "x"}
        assert REDACTED not in json.dumps(cfg.to_payload())

    def test_root_only_snapshot_imports_to_an_empty_design(self) -> None:
        snap = Snapshot(
            scope="uni",
            tree={"class": "polUni", "rn": "uni", "attributes": {}, "children": []},
        )
        cfg = to_design(snap)
        assert cfg.design_node.children == []

    def test_duplicate_dn_in_raw_direct_is_a_structure_problem(self) -> None:
        snap = _tenant_snapshot()
        assert snap.tree is not None
        twin = {"class": "fvWeird", "rn": "w-x", "attributes": {}, "children": []}
        snap.tree["children"][0]["children"] += [twin, dict(twin)]
        with pytest.raises(SnapshotImportError) as exc:
            to_design(snap, on_unknown="raw")
        assert [p.kind for p in exc.value.problems] == ["structure"]


# ── Structure guards ──────────────────────────────────────────────────────────


class TestStructureGuards:
    def test_scope_outside_uni_is_refused(self) -> None:
        # Scoped imports under uni are legal since it.3; anything OUTSIDE
        # the config universe still refuses (operational trees never import).
        snap = Snapshot(
            scope="topology/pod-1",
            tree={"class": "fabricPod", "rn": "pod-1", "attributes": {}, "children": []},
        )
        with pytest.raises(DesignError, match="outside"):
            to_design(snap)

    def test_empty_snapshot_is_refused(self) -> None:
        with pytest.raises(DesignError, match="empty"):
            to_design(Snapshot(scope="uni", tree=None))

    def test_non_poluni_root_is_refused(self) -> None:
        snap = Snapshot(
            scope="uni",
            tree={"class": "fvTenant", "rn": "tn-x", "attributes": {}, "children": []},
        )
        with pytest.raises(DesignError, match="polUni"):
            to_design(snap)

    def test_rn_format_mismatch_is_collected(self) -> None:
        snap = _tenant_snapshot()
        assert snap.tree is not None
        snap.tree["children"][0]["rn"] = "weird-prod"
        with pytest.raises(SnapshotImportError) as exc:
            to_design(snap)
        assert [p.kind for p in exc.value.problems] == ["structure"]

    def test_input_snapshot_is_not_mutated(self) -> None:
        snap = _tenant_snapshot()
        before = copy.deepcopy(snap.tree)
        to_design(snap)
        assert snap.tree == before


# ── Inversion-table guards (the measured facts, pinned) ───────────────────────


class TestInversionGuards:
    def test_maker_inversion_is_a_bijection(self) -> None:
        """845 curated makers, zero (parent, child) collisions — measured.

        Should curation ever add a second maker for one pair, this fails and
        forces a conscious tie-break in ``_maker_inversion``.
        """
        import yaml

        from niwaki.design._cursor import _VOCABULARY_YAML

        with _VOCABULARY_YAML.open(encoding="utf-8") as fh:
            makers = yaml.safe_load(fh).get("makers", {})
        pairs = [(parent, child) for parent, table in makers.items() for child in table.values()]
        assert len(pairs) == len(set(pairs))
        assert len(_maker_inversion()) == len(pairs)

    def test_verb_inversion_is_unique(self) -> None:
        """No two verbs on one owner share an Rs class — the reason verbs exist."""
        import yaml

        from niwaki.design._cursor import _VOCABULARY_YAML

        with _VOCABULARY_YAML.open(encoding="utf-8") as fh:
            verbs = yaml.safe_load(fh).get("verbs", {})
        declared = [
            (owner, spec["rs"]) for owner, table in verbs.items() for spec in table.values()
        ]
        assert len(declared) == len(set(declared))
        assert set(_verb_inversion()) == set(declared)
