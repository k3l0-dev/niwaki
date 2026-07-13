"""Design roots — design()/tenant()/infra()/fabric()/controller() factories.

Every design is rooted at ``polUni``; the root factories are
sugar for ``design().<maker>()`` and multi-domain designs are structural —
nominal path, edge cases, robustness.  No I/O anywhere.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from niwaki.design import controller, design, fabric, infra, tenant
from niwaki.design._generated_cursors import (
    ControllerCursor,
    FabricCursor,
    InfraCursor,
    TenantCursor,
    UniCursor,
)
from niwaki.exceptions import DesignError, DuplicateDeclarationError


class TestDesignFactory:
    def test_returns_empty_uni_root(self) -> None:
        cfg = design()
        assert type(cfg) is UniCursor
        assert cfg.design_node.aci_class == "polUni"
        assert cfg.design_node.parent is None
        assert cfg.design_node.position == ""
        assert cfg.design_node.children == []
        assert cfg.dn == "uni"

    def test_root_makers_declare_domains(self) -> None:
        cfg = design()
        assert type(cfg.tenant("prod")) is TenantCursor
        assert type(cfg.infra()) is InfraCursor
        assert type(cfg.fabric()) is FabricCursor
        assert type(cfg.controller()) is ControllerCursor
        assert [c.aci_class for c in cfg.design_node.children] == [
            "fvTenant",
            "infraInfra",
            "fabricInst",
            "ctrlrInst",
        ]

    def test_phys_dom_and_l3_dom_are_root_makers(self) -> None:
        cfg = design()
        assert cfg.phys_dom("prod-phys").dn == "uni/phys-prod-phys"
        assert cfg.l3_dom("prod-l3").dn == "uni/l3dom-prod-l3"

    def test_duplicate_domain_raises(self) -> None:
        cfg = design()
        cfg.infra()
        with pytest.raises(DuplicateDeclarationError):
            cfg.infra()

    def test_empty_design_compiles_to_bare_envelope(self) -> None:
        assert design().to_payload() == {"polUni": {"attributes": {}}}


class TestDomainShorthands:
    def test_tenant_is_design_dot_tenant(self) -> None:
        cfg = tenant("prod", description="production")
        assert type(cfg) is TenantCursor
        assert cfg.dn == "uni/tn-prod"
        root = cfg.design_node.root()
        assert root.aci_class == "polUni"
        assert root.position == ""
        assert cfg.design_node.attrs == {"description": "production"}

    def test_infra_singleton_root(self) -> None:
        cfg = infra()
        assert type(cfg) is InfraCursor
        assert cfg.dn == "uni/infra"
        assert cfg.design_node.position == "infra"

    def test_fabric_singleton_root(self) -> None:
        cfg = fabric()
        assert type(cfg) is FabricCursor
        assert cfg.dn == "uni/fabric"

    def test_controller_singleton_root(self) -> None:
        cfg = controller()
        assert type(cfg) is ControllerCursor
        assert cfg.dn == "uni/controller"

    def test_invalid_tenant_name_raises_immediately(self) -> None:
        with pytest.raises(ValidationError):
            tenant("bad name with spaces")


class TestCrossDomainPop:
    def test_sibling_domain_from_domain_cursor(self) -> None:
        """.fabric() from an infra cursor lands under the same polUni root."""
        i = infra()
        f = i.fabric()
        assert type(f) is FabricCursor
        assert f.design_node.root() is i.design_node.root()
        assert [c.aci_class for c in i.design_node.root().children] == ["infraInfra", "fabricInst"]

    def test_sibling_domain_from_deep_cursor(self) -> None:
        """Implicit pop crosses domains from any depth."""
        pool = infra().vlan_pool("prod", "static")
        t = pool.tenant("prod")
        assert type(t) is TenantCursor
        assert t.design_node.root() is pool.design_node.root()

    def test_multi_domain_payload_is_one_envelope(self) -> None:
        cfg = design()
        cfg.fabric()
        cfg.infra()
        cfg.tenant("prod")
        payload = cfg.to_payload()
        classes = [next(iter(child)) for child in payload["polUni"]["children"]]
        assert classes == ["fabricInst", "infraInfra", "fvTenant"]


class TestPositionDispatch:
    def test_same_class_two_positions_two_cursor_types(self) -> None:
        """infraNodeBlk cursors are position-typed, not class-typed."""
        leaf_blk = (
            infra()
            .leaf_profile("lp")
            .leaf_selector("ls", "range")
            .node_block("b1", from_node_id="101", to_node_id="101")
        )
        spine_blk = (
            infra()
            .spine_profile("sp")
            .spine_selector("ss", "range")
            .node_block("b1", from_node_id="201", to_node_id="201")
        )
        assert type(leaf_blk).__name__ == "LeafSelectorNodeBlockCursor"
        assert type(spine_blk).__name__ == "SpineSelectorNodeBlockCursor"
        assert leaf_blk.design_node.position == "infra.leaf_profile.leaf_selector.node_block"
        assert spine_blk.design_node.position == "infra.spine_profile.spine_selector.node_block"

    def test_mo_escape_hatch_is_positionless(self) -> None:
        """Uncurated nodes fall back to the base Cursor (no typed position)."""
        from niwaki.design import Cursor
        from niwaki.models._generated.tag.tagTag import tagTag

        tag = tenant("prod").mo(tagTag, key="env", value="prod")
        assert type(tag) is Cursor
        assert tag.design_node.position is None

    def test_multi_prop_naming_positional(self) -> None:
        """vlan_pool(name, allocation_mode) — naming props map positionally."""
        pool = infra().vlan_pool("prod", "static")
        assert pool.dn == "uni/infra/vlanns-[prod]-static"
        blk = pool.range("vlan-100", "vlan-199")
        assert blk.dn == "uni/infra/vlanns-[prod]-static/from-[vlan-100]-to-[vlan-199]"

    def test_unknown_maker_still_suggests(self) -> None:
        with pytest.raises(DesignError, match="vlan_pool"):
            infra().vlan_pool_("prod", "static")
