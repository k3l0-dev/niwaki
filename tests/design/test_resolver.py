"""Reference resolution — closed world, forward refs, direction, conflicts."""

from __future__ import annotations

import pytest

from niwaki.design import tenant
from niwaki.design._node import DesignNode, PendingBind
from niwaki.design._resolver import _lookup_target, build_index, resolve
from niwaki.exceptions import (
    AmbiguousBindError,
    DuplicateDeclarationError,
    UnresolvedReferenceError,
)


class TestClosedWorld:
    def test_forward_reference_resolves(self) -> None:
        """bind() may reference an object declared later in the design."""
        cfg = tenant("prod")
        bd = cfg.bd("web").bind(vrf="prod")
        cfg.vrf("prod")  # declared after the bind
        extras = resolve(cfg.design_node.root())
        (rs,) = extras[bd.design_node]
        assert rs._aci_class == "fvRsCtx"
        assert rs.to_apic()["fvRsCtx"]["attributes"]["tnFvCtxName"] == "prod"

    def test_unresolved_target_raises_with_suggestion(self) -> None:
        cfg = tenant("prod")
        cfg.bd("web").bind(vrf="prdo")  # typo
        cfg.vrf("prod")
        with pytest.raises(UnresolvedReferenceError, match="Did you mean 'prod'"):
            resolve(cfg.design_node.root())

    def test_unresolved_lists_declared_instances(self) -> None:
        cfg = tenant("prod")
        cfg.app("a").epg("web").consume("nope")
        cfg.contract("http")
        with pytest.raises(UnresolvedReferenceError, match="Declared: http"):
            resolve(cfg.design_node.root())

    def test_wrong_class_does_not_satisfy_reference(self) -> None:
        """A BD named like the missing VRF must not satisfy bind(vrf=...)."""
        cfg = tenant("prod")
        cfg.bd("shared").bind(vrf="shared")
        with pytest.raises(UnresolvedReferenceError):
            resolve(cfg.design_node.root())


class TestDirection:
    def test_direct_edge_attaches_on_owner(self) -> None:
        cfg = tenant("prod")
        epg = cfg.app("a").epg("web").bind(bd="web")
        cfg.bd("web")
        extras = resolve(cfg.design_node.root())
        (rs,) = extras[epg.design_node]
        assert rs._aci_class == "fvRsBd"

    def test_inverse_edge_attaches_on_target(self) -> None:
        """vrf.bind(l3out=...) creates l3extRsEctx under the L3Out."""
        cfg = tenant("prod")
        vrf = cfg.vrf("prod").bind(l3out="ext")
        l3out = cfg.l3out("ext")
        extras = resolve(cfg.design_node.root())
        assert vrf.design_node not in extras
        (rs,) = extras[l3out.design_node]
        assert rs._aci_class == "l3extRsEctx"
        # The Rs points back at the owner VRF.
        assert rs.to_apic()["l3extRsEctx"]["attributes"]["tnFvCtxName"] == "prod"

    def test_no_rs_class_in_either_direction(self) -> None:
        cfg = tenant("prod")
        bd = cfg.bd("web")
        cfg.app("a")
        # Hand-crafted impossible edge: no Rs exists between fvBD and fvAp.
        bd.design_node.binds.append(
            PendingBind(kind="bind", alias="app", target_aci_class="fvAp", target_name="a")
        )
        with pytest.raises(AmbiguousBindError, match="either direction"):
            resolve(cfg.design_node.root())


class TestConflicts:
    def test_double_singleton_bind_raises(self) -> None:
        cfg = tenant("prod")
        cfg.bd("web").bind(vrf="a").bind(vrf="b")
        cfg.vrf("a")
        cfg.vrf("b")
        with pytest.raises(DuplicateDeclarationError, match="declared twice"):
            resolve(cfg.design_node.root())

    def test_two_named_rs_on_same_epg_are_fine(self) -> None:
        cfg = tenant("prod")
        epg = cfg.app("a").epg("web").provide("http").provide("https")
        cfg.contract("http")
        cfg.contract("https")
        extras = resolve(cfg.design_node.root())
        assert [rs.rn for rs in extras[epg.design_node]] == ["rsprov-http", "rsprov-https"]

    def test_ambiguous_index_entry_raises(self) -> None:
        """Two design nodes sharing (class, name) make that name unbindable."""
        from niwaki.design._node import PendingBind
        from niwaki.design._resolver import _AMBIGUOUS, _Ambiguous

        index: dict[str, dict[str, DesignNode | _Ambiguous]] = {"fvCtx": {"prod": _AMBIGUOUS}}
        owner = tenant("t").design_node
        bind = PendingBind(kind="bind", alias="vrf", target_aci_class="fvCtx", target_name="prod")
        with pytest.raises(AmbiguousBindError, match="more than one"):
            _lookup_target(index, owner, bind)


class TestIndex:
    def test_index_covers_named_nodes_only(self) -> None:
        cfg = tenant("prod")
        cfg.vrf("v").pim()  # pimCtxP is a singleton — no primary name
        index = build_index(cfg.design_node.root())
        assert index["fvCtx"]["v"] is not None
        assert "pimCtxP" not in index

    def test_resolution_does_not_mutate_design(self) -> None:
        """resolve() twice yields the same result — the tree is untouched."""
        cfg = tenant("prod")
        bd = cfg.bd("web").bind(vrf="prod")
        cfg.vrf("prod")
        first = resolve(cfg.design_node.root())
        second = resolve(cfg.design_node.root())
        assert len(first[bd.design_node]) == len(second[bd.design_node]) == 1
        assert bd.design_node.children == []
