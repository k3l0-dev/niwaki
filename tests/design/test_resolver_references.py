"""Unified reference resolver (ADR-001 phase 2) — flavors, abstracts, bind_dn.

Complements ``test_resolver.py`` (name-flavor tenant world) with the phase-2
surface: dn-flavor relations, abstract-target matching, the ``bind_dn`` raw-DN
escape, and its pedagogical failure modes.  No I/O anywhere.
"""

from __future__ import annotations

import pytest

from niwaki.design import design, infra, tenant
from niwaki.design._resolver import resolve
from niwaki.exceptions import (
    AmbiguousBindError,
    DesignError,
    DuplicateDeclarationError,
    UnresolvedReferenceError,
)


class TestDnFlavor:
    def test_direct_dn_relation_stores_target_dn(self) -> None:
        cfg = infra()
        cfg.vlan_pool("prod", "static")
        dom = cfg.phys_dom("prod-phys").bind(vlan_pool="prod")
        extras = resolve(dom.design_node.root())
        (rs,) = extras[dom.design_node]
        assert type(rs).__name__ == "infraRsVlanNs"
        assert rs.target_dn == "uni/infra/vlanns-[prod]-static"

    def test_forward_reference_resolves(self) -> None:
        """The pool is declared *after* the bind — closed world, not ordering."""
        cfg = infra()
        dom = cfg.phys_dom("prod-phys").bind(vlan_pool="prod")
        cfg.vlan_pool("prod", "static")
        extras = resolve(dom.design_node.root())
        assert extras[dom.design_node][0].target_dn == "uni/infra/vlanns-[prod]-static"

    def test_name_flavor_still_stores_name(self) -> None:
        cfg = infra()
        cfg.cdp_policy("cdp-on")
        grp = cfg.func_profile().access_group("pg").bind(cdp="cdp-on")
        extras = resolve(grp.design_node.root())
        (rs,) = extras[grp.design_node]
        assert type(rs).__name__ == "infraRsCdpIfPol"
        assert rs.name == "cdp-on"

    def test_unresolved_dn_target_raises(self) -> None:
        cfg = infra()
        dom = cfg.phys_dom("prod-phys").bind(vlan_pool="missing")
        with pytest.raises(UnresolvedReferenceError, match="missing"):
            resolve(dom.design_node.root())


class TestAbstractTargets:
    def test_abstract_alias_matches_declared_concrete(self) -> None:
        """aaep.bind(domain=...) targets abstract infraADomP; physDomP matches."""
        cfg = design()
        cfg.phys_dom("prod-phys")
        aaep = cfg.infra().aaep("prod-aaep").bind(domain="prod-phys")
        extras = resolve(aaep.design_node.root())
        (rs,) = extras[aaep.design_node]
        assert type(rs).__name__ == "infraRsDomP"
        assert rs.target_dn == "uni/phys-prod-phys"

    def test_cross_domain_epg_to_phys_dom(self) -> None:
        """The EPG (tenant domain) binds the phys-dom declared under uni."""
        cfg = design()
        cfg.phys_dom("prod-phys")
        epg = cfg.tenant("prod").app("a").epg("web").bind(domain="prod-phys")
        extras = resolve(epg.design_node.root())
        (rs,) = extras[epg.design_node]
        assert type(rs).__name__ == "fvRsDomAtt"
        assert rs.target_dn == "uni/phys-prod-phys"

    def test_policy_group_abstract_resolves_to_access_group(self) -> None:
        cfg = infra()
        cfg.func_profile().access_group("server-pg")
        sel = cfg.access_port_profile("p").port_selector("1.01", "range")
        sel.bind(policy_group="server-pg")
        extras = resolve(sel.design_node.root())
        (rs,) = extras[sel.design_node]
        assert rs.target_dn == "uni/infra/funcprof/accportgrp-server-pg"

    def test_two_concrete_subclasses_same_name_is_ambiguous(self) -> None:
        """A phys-dom and an l3-dom named alike make domain= unresolvable."""
        cfg = design()
        cfg.phys_dom("prod")
        cfg.l3_dom("prod")
        aaep = cfg.infra().aaep("prod-aaep").bind(domain="prod")
        with pytest.raises(AmbiguousBindError, match="physDomP"):
            resolve(aaep.design_node.root())

    def test_miss_lists_declared_names_with_suggestion(self) -> None:
        cfg = design()
        cfg.phys_dom("prod-phys")
        aaep = cfg.infra().aaep("prod-aaep").bind(domain="prod-hys")
        with pytest.raises(UnresolvedReferenceError, match="Did you mean 'prod-phys'"):
            resolve(aaep.design_node.root())


class TestBindDn:
    def test_passthrough_without_lookup(self) -> None:
        """The referenced pool is NOT in the design — bind_dn trusts the DN."""
        dom = design().phys_dom("prod-phys")
        dom.bind_dn(vlan_pool="uni/infra/vlanns-[shared]-static")
        extras = resolve(dom.design_node.root())
        (rs,) = extras[dom.design_node]
        assert type(rs).__name__ == "infraRsVlanNs"
        assert rs.target_dn == "uni/infra/vlanns-[shared]-static"

    def test_typed_cursor_exposes_dn_aliases_only(self) -> None:
        import inspect

        from niwaki.design._generated_cursors import AccessGroupCursor

        params = set(inspect.signature(AccessGroupCursor.bind_dn).parameters)
        assert "aaep" in params  # dn flavor
        assert "cdp" not in params  # name flavor — bind() only

    def test_name_flavor_alias_is_refused_natively_on_typed_cursor(self) -> None:
        """Typed bind_dn signatures only expose dn-flavor aliases."""
        grp = infra().func_profile().access_group("pg")
        with pytest.raises(TypeError):
            grp.bind_dn(cdp="uni/infra/cdpIfP-on")  # type: ignore[call-arg]

    def test_name_flavor_alias_is_refused_on_dynamic_path(self) -> None:
        from niwaki.design import Cursor

        grp = infra().func_profile().access_group("pg")
        with pytest.raises(DesignError, match="targets by name"):
            Cursor.bind_dn(grp, cdp="uni/infra/cdpIfP-on")

    def test_unknown_alias_is_refused(self) -> None:
        from niwaki.design import Cursor

        dom = design().phys_dom("prod-phys")
        with pytest.raises(DesignError, match="No bind alias"):
            Cursor.bind_dn(dom, nonsense="uni/x")

    def test_collides_with_resolved_bind_on_same_rn(self) -> None:
        """bind() and bind_dn() on the same singleton Rs collide loudly."""
        cfg = infra()
        cfg.vlan_pool("prod", "static")
        dom = cfg.phys_dom("prod-phys").bind(vlan_pool="prod")
        dom.bind_dn(vlan_pool="uni/infra/vlanns-[other]-static")
        with pytest.raises(DuplicateDeclarationError, match="declared twice"):
            resolve(dom.design_node.root())


class TestStaticPathMaker:
    def test_literal_dn_maker(self) -> None:
        """fvRsPathAtt is a maker (C-11): the path DN is naming, not a bind."""
        epg = tenant("prod").app("a").epg("web")
        path = epg.static_path("topology/pod-1/paths-101/pathep-[eth1/1]", encap="vlan-100")
        assert path.design_node.aci_class == "fvRsPathAtt"
        assert path.design_node.naming == {"target_dn": "topology/pod-1/paths-101/pathep-[eth1/1]"}
        assert path.design_node.attrs == {"encap": "vlan-100"}
