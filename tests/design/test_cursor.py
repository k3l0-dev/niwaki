"""Design cursor construction — makers, implicit pop, set(), errors.

Phase A coverage: nominal path, edge cases, robustness.  No I/O anywhere.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from niwaki.design import Cursor, tenant
from niwaki.exceptions import (
    DesignError,
    DuplicateDeclarationError,
    UnknownMakerError,
)
from niwaki.models._generated.fv.fvRsCtx import fvRsCtx
from niwaki.models._generated.tag.tagTag import tagTag


class TestTenantRoot:
    def test_returns_root_cursor(self) -> None:
        cfg = tenant("prod")
        assert isinstance(cfg, Cursor)
        # Every design is rooted on a uniform polUni node.
        assert cfg.design_node.parent is not None
        assert cfg.design_node.parent.aci_class == "polUni"
        assert cfg.design_node.parent.parent is None
        assert cfg.design_node.aci_class == "fvTenant"
        assert cfg.dn == "uni/tn-prod"

    def test_attrs_accepted(self) -> None:
        cfg = tenant("prod", description="production tenant")
        assert cfg.design_node.attrs == {"description": "production tenant"}

    def test_invalid_name_raises_immediately(self) -> None:
        with pytest.raises(ValidationError):
            tenant("bad name with spaces")

    def test_invalid_attr_raises_immediately(self) -> None:
        with pytest.raises(ValidationError):
            tenant("prod", description="x" * 500)


class TestMakers:
    def test_maker_creates_child_and_returns_child_cursor(self) -> None:
        bd = tenant("prod").bd("web")
        assert bd.design_node.aci_class == "fvBD"
        assert bd.design_node.primary_name == "web"
        assert bd.design_node.parent is not None
        assert bd.design_node.parent.aci_class == "fvTenant"
        assert bd.dn == "uni/tn-prod/BD-web"

    def test_maker_kwargs_shorthand_for_attrs(self) -> None:
        bd = tenant("prod").bd("web", unicast_routing=True)
        assert bd.design_node.attrs == {"unicast_routing": True}

    def test_naming_by_keyword(self) -> None:
        subnet = tenant("prod").bd("web").subnet(subnet="10.0.1.1/24")
        assert subnet.design_node.naming == {"subnet": "10.0.1.1/24"}
        assert subnet.design_node.rn == "subnet-[10.0.1.1/24]"

    def test_singleton_maker_takes_no_naming(self) -> None:
        pim = tenant("prod").vrf("prod").pim()
        assert pim.design_node.aci_class == "pimCtxP"
        assert pim.design_node.rn == "pimctxp"

    def test_too_many_positional_args(self) -> None:
        """Typed signatures reject extra positional args natively."""
        with pytest.raises(TypeError):
            tenant("prod").bd("web", "extra")  # type: ignore[call-arg]

    def test_too_many_positional_args_dynamic_path(self) -> None:
        """The dynamic runtime keeps its own guard (used by .mo()/uncurated)."""
        with pytest.raises(DesignError, match="at most"):
            tenant("prod")._invoke_maker("bd", ("web", "extra"), {})

    def test_naming_given_twice(self) -> None:
        """Typed signatures reject a doubled naming prop natively."""
        with pytest.raises(TypeError):
            tenant("prod").bd("web", name="other")  # type: ignore[call-arg]

    def test_naming_given_twice_dynamic_path(self) -> None:
        with pytest.raises(DesignError, match="positionally and by keyword"):
            tenant("prod")._invoke_maker("bd", ("web",), {"name": "other"})

    def test_invalid_naming_value(self) -> None:
        with pytest.raises(ValidationError):
            tenant("prod").bd("bad name")

    def test_cursors_are_capturable_values(self) -> None:
        cfg = tenant("prod")
        app = cfg.app("main")
        for tier in ("frontend", "backend"):
            app.epg(tier)
        assert [c.primary_name for c in app.design_node.children] == ["frontend", "backend"]


class TestImplicitPop:
    def test_sibling_from_child_level(self) -> None:
        """.epg() then .epg() — second is a sibling under the same app."""
        epg2 = tenant("prod").app("main").epg("a").epg("b")
        app = epg2.design_node.parent
        assert app is not None
        assert app.aci_class == "fvAp"
        assert [c.primary_name for c in app.children] == ["a", "b"]

    def test_pop_to_tenant_level(self) -> None:
        """.bd() from an EPG cursor creates under the tenant."""
        bd = tenant("prod").app("main").epg("web").bd("web-bd")
        assert bd.design_node.parent is not None
        assert bd.design_node.parent.aci_class == "fvTenant"

    def test_unknown_maker_raises_with_suggestion(self) -> None:
        with pytest.raises(UnknownMakerError, match="Did you mean 'epg'"):
            tenant("prod").app("main").epgg("web")

    def test_unknown_maker_is_attribute_error(self) -> None:
        assert not hasattr(tenant("prod"), "nonexistent_maker")

    def test_private_attribute_raises_plain_attribute_error(self) -> None:
        with pytest.raises(AttributeError):
            _ = tenant("prod")._private_thing


class TestSet:
    def test_set_merges_and_last_wins(self) -> None:
        bd = tenant("prod").bd("web").set(unicast_routing=True, multicast_allow=True)
        bd.set(unicast_routing=False)
        assert bd.design_node.attrs == {"unicast_routing": False, "multicast_allow": True}

    def test_set_returns_same_cursor(self) -> None:
        bd = tenant("prod").bd("web")
        assert bd.set(unicast_routing=True) is bd

    def test_set_invalid_value_raises_and_preserves_state(self) -> None:
        bd = tenant("prod").bd("web").set(unicast_routing=True)
        with pytest.raises(ValidationError):
            bd.set(description="\x00 invalid")
        assert bd.design_node.attrs == {"unicast_routing": True}


class TestAttrNameValidation:
    """Bad attribute names fail loudly on both dispatch paths.

    Typed cursors reject unknown keywords natively (TypeError); the dynamic
    runtime (``.mo()`` escape hatch, uncurated classes) applies its own
    validation with redirects and suggestions — extra="allow" on the models
    would otherwise silently absorb typos into model_extra.
    """

    def test_typed_cursor_rejects_unknown_kwarg_natively(self) -> None:
        with pytest.raises(TypeError):
            tenant("prod").bd("web").set(arpFlood=True)  # type: ignore[call-arg]

    def test_wire_alias_redirects_to_python_name(self) -> None:
        from niwaki.design._cursor import Cursor

        bd = tenant("prod").bd("web")
        with pytest.raises(DesignError, match="use the Python field name 'arp_flooding'"):
            Cursor.set(bd, arpFlood=True)

    def test_unknown_attribute_suggests_closest(self) -> None:
        from niwaki.design._cursor import Cursor

        bd = tenant("prod").bd("web")
        with pytest.raises(DesignError, match="did you mean 'unicast_routing'"):
            Cursor.set(bd, unicast_routin=True)

    def test_unknown_attribute_at_dynamic_maker(self) -> None:
        with pytest.raises(DesignError, match="has no attribute"):
            tenant("prod")._invoke_maker("bd", ("web",), {"not_a_field": True})

    def test_naming_prop_rejected_in_set(self) -> None:
        from niwaki.design._cursor import Cursor

        bd = tenant("prod").bd("web")
        with pytest.raises(DesignError, match="fixed at creation"):
            Cursor.set(bd, name="other")


class TestDuplicates:
    def test_same_object_twice_raises(self) -> None:
        cfg = tenant("prod")
        cfg.bd("web")
        with pytest.raises(DuplicateDeclarationError, match="already declared"):
            cfg.bd("web")

    def test_same_name_different_class_is_fine(self) -> None:
        cfg = tenant("prod")
        cfg.bd("prod")
        cfg.vrf("prod")
        assert len(cfg.design_node.children) == 2

    def test_same_name_different_parent_is_fine(self) -> None:
        cfg = tenant("prod")
        cfg.app("a").epg("web")
        cfg.app("b").epg("web")


class TestMoEscapeHatch:
    def test_valid_child_class(self) -> None:
        tag = tenant("prod").mo(tagTag, key="env", value="prod")
        assert tag.design_node.aci_class == "tagTag"
        assert tag.design_node.naming == {"key": "env"}
        assert tag.design_node.attrs == {"value": "prod"}

    def test_containment_violation_raises(self) -> None:
        # fvRsCtx lives under a BD, never directly under a tenant.
        with pytest.raises(DesignError, match="not a valid APIC child"):
            tenant("prod").mo(fvRsCtx, name="prod")


class TestBindDeclaration:
    def test_bind_records_pending_on_owner(self) -> None:
        bd = tenant("prod").bd("web").bind(vrf="prod")
        (pending,) = bd.design_node.binds
        assert pending.kind == "bind"
        assert pending.alias == "vrf"
        assert pending.target_aci_class == "fvCtx"
        assert pending.target_name == "prod"

    def test_bind_walks_up_from_subnet_to_bd(self) -> None:
        bd = tenant("prod").bd("web")
        bd.subnet("10.0.1.1/24").bind(vrf="prod")
        assert len(bd.design_node.binds) == 1

    def test_bind_returns_calling_cursor(self) -> None:
        subnet = tenant("prod").bd("web").subnet("10.0.1.1/24")
        assert subnet.bind(vrf="prod") is subnet

    def test_unknown_alias_raises_natively_on_typed_cursor(self) -> None:
        """Typed bind() rejects unknown aliases at the call site."""
        with pytest.raises(TypeError):
            tenant("prod").bd("web").bind(nope="x")  # type: ignore[call-arg]

    def test_unknown_alias_raises_on_dynamic_path(self) -> None:
        from niwaki.design._cursor import Cursor

        bd = tenant("prod").bd("web")
        with pytest.raises(DesignError, match="No bind alias 'nope'"):
            Cursor.bind(bd, nope="x")

    def test_provide_consume_record_on_epg(self) -> None:
        epg = tenant("prod").app("a").epg("web").provide("http").consume("db")
        kinds = [(b.kind, b.rs_aci_class, b.target_name) for b in epg.design_node.binds]
        assert kinds == [("provide", "fvRsProv", "http"), ("consume", "fvRsCons", "db")]

    def test_provide_outside_epg_raises(self) -> None:
        with pytest.raises(DesignError, match="contract verbs apply to EPGs"):
            tenant("prod").bd("web").provide("http")


class TestRepr:
    def test_repr_shows_path(self) -> None:
        bd = tenant("prod").bd("web")
        assert repr(bd) == "<Cursor uni → tenant 'prod' → bd 'web'>"
