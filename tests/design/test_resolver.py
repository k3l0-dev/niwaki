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

    def test_same_scope_duplicate_name_is_ambiguous(self) -> None:
        """Two same-class nodes sharing (scope, name) stay unbindable.

        The makers reject this structurally (a name is declared once per
        parent), so it is assembled directly to prove the tie-breaker: two
        equally-near candidates fail loudly rather than one silently winning.
        """
        from niwaki.design._cursor import _load_class

        root = tenant("prod").design_node.root()
        parent = root.children[0]  # the fvTenant node
        fvCtx = _load_class("fvCtx")
        parent.children.append(DesignNode(fvCtx, "vrf", {"name": "dup"}, {}, parent))
        parent.children.append(DesignNode(fvCtx, "vrf", {"name": "dup"}, {}, parent))
        index = build_index(root)
        bind = PendingBind(kind="bind", alias="vrf", target_aci_class="fvCtx", target_name="dup")
        with pytest.raises(AmbiguousBindError, match="same scope"):
            _lookup_target(index, parent, bind)


class TestScoping:
    """R1 — a name reused across tenants resolves to the owner's own scope."""

    def test_same_name_across_tenants_resolves_locally(self) -> None:
        from niwaki.design import design

        cfg = design()
        a = cfg.tenant("a")
        vrf_a = a.vrf("prod")
        bd_a = a.bd("web").bind(vrf="prod")
        b = cfg.tenant("b")
        b.vrf("prod")
        b.bd("web").bind(vrf="prod")

        # Neither bind is ambiguous any more — resolution succeeds for both.
        extras = resolve(cfg.design_node.root())
        assert len(extras[bd_a.design_node]) == 1

        # And the winning target is tenant a's own VRF, not tenant b's.
        index = build_index(cfg.design_node.root())
        bind = PendingBind(kind="bind", alias="vrf", target_aci_class="fvCtx", target_name="prod")
        assert _lookup_target(index, bd_a.design_node, bind) is vrf_a.design_node


class TestIndex:
    def test_index_covers_named_nodes_only(self) -> None:
        cfg = tenant("prod")
        cfg.vrf("v").pim()  # pimCtxP is a singleton — no primary name
        index = build_index(cfg.design_node.root())
        assert [node.primary_name for node in index["fvCtx"]["v"]] == ["v"]
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
