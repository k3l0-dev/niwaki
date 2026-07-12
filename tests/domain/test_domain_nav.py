"""Tests for NiwakiNode jargon navigation (__getattr__ domain layer).

Validates that short method names resolve to the correct ACI classes and
compute the right DNs, without any network calls.
"""

from __future__ import annotations

import pytest

from niwaki import Niwaki
from niwaki.domain._child_map import CHILD_MAP, RS_TARGET_PROP
from niwaki.models._generated.fv.fvRsBd import fvRsBd
from niwaki.models._generated.fv.fvRsCtx import fvRsCtx
from niwaki.models._generated.fv.fvRsCustQosPol import fvRsCustQosPol
from niwaki.models._generated.fv.fvTenant import fvTenant
from niwaki.models._generated.infra.infraAttEntityP import infraAttEntityP
from niwaki.models._generated.infra.infraInfra import infraInfra
from niwaki.models._generated.phys.physDomP import physDomP


@pytest.fixture()
def aci(mock_niwaki: Niwaki) -> Niwaki:
    return mock_niwaki


@pytest.fixture()
def mock_niwaki() -> Niwaki:
    """A Niwaki instance wired to a fake session — no network needed."""
    from unittest.mock import MagicMock

    session = MagicMock()
    niwaki = Niwaki(session)
    return niwaki


# ── Child map structure ───────────────────────────────────────────────────────


class TestChildMap:
    def test_root_has_tenant(self) -> None:
        assert CHILD_MAP["_root"]["tenant"] == "fvTenant"

    def test_root_has_infra(self) -> None:
        assert CHILD_MAP["_root"]["infra"] == "infraInfra"

    def test_root_has_phys_dom(self) -> None:
        assert CHILD_MAP["_root"]["phys_dom"] == "physDomP"

    def test_tenant_has_bd(self) -> None:
        assert CHILD_MAP["fvTenant"]["bd"] == "fvBD"

    def test_tenant_has_vrf(self) -> None:
        assert CHILD_MAP["fvTenant"]["vrf"] == "fvCtx"

    def test_tenant_has_app(self) -> None:
        assert CHILD_MAP["fvTenant"]["app"] == "fvAp"

    def test_tenant_has_contract(self) -> None:
        assert CHILD_MAP["fvTenant"]["contract"] == "vzBrCP"

    def test_app_has_epg(self) -> None:
        assert CHILD_MAP["fvAp"]["epg"] == "fvAEPg"

    def test_bd_has_subnet(self) -> None:
        assert CHILD_MAP["fvBD"]["subnet"] == "fvSubnet"

    def test_infra_has_aaep(self) -> None:
        assert CHILD_MAP["infraInfra"]["aaep"] == "infraAttEntityP"

    def test_infra_has_vlan_pool(self) -> None:
        assert CHILD_MAP["infraInfra"]["vlan_pool"] == "fvnsVlanInstP"

    # RS_TARGET_PROP
    def test_rs_target_prop_fv_rs_bd(self) -> None:
        assert RS_TARGET_PROP["fvRsBd"] == "tnFvBDName"

    def test_rs_target_prop_fv_rs_ctx(self) -> None:
        assert RS_TARGET_PROP["fvRsCtx"] == "tnFvCtxName"

    def test_rs_target_prop_fv_rs_cust_qos_pol(self) -> None:
        assert RS_TARGET_PROP["fvRsCustQosPol"] == "tnQosCustomPolName"

    def test_rs_target_prop_excludes_named_rs(self) -> None:
        # fvRsCons has identifiedBy=[tnVzBrCPName], not a singleton → not in map
        assert "fvRsCons" not in RS_TARGET_PROP


# ── NiwakiNode.__getattr__ ────────────────────────────────────────────────────


class TestNiwakiNodeGetattr:
    def test_tenant_dn(self, mock_niwaki: Niwaki) -> None:
        node = mock_niwaki.root.tenant("prod")
        assert node.dn == "uni/tn-prod"

    def test_tenant_cls(self, mock_niwaki: Niwaki) -> None:
        node = mock_niwaki.root.tenant("prod")
        assert node.cls is fvTenant

    def test_bd_dn(self, mock_niwaki: Niwaki) -> None:
        node = mock_niwaki.root.tenant("prod").bd("web")
        assert node.dn == "uni/tn-prod/BD-web"

    def test_vrf_dn(self, mock_niwaki: Niwaki) -> None:
        node = mock_niwaki.root.tenant("prod").vrf("main")
        assert node.dn == "uni/tn-prod/ctx-main"

    def test_app_dn(self, mock_niwaki: Niwaki) -> None:
        node = mock_niwaki.root.tenant("prod").app("myapp")
        assert node.dn == "uni/tn-prod/ap-myapp"

    def test_epg_dn(self, mock_niwaki: Niwaki) -> None:
        node = mock_niwaki.root.tenant("prod").app("myapp").epg("frontend")
        assert node.dn == "uni/tn-prod/ap-myapp/epg-frontend"

    def test_subnet_dn(self, mock_niwaki: Niwaki) -> None:
        node = mock_niwaki.root.tenant("prod").bd("web").subnet(ip="10.0.0.1/24")
        assert node.dn == "uni/tn-prod/BD-web/subnet-[10.0.0.1/24]"

    def test_infra_dn(self, mock_niwaki: Niwaki) -> None:
        node = mock_niwaki.root.infra()
        assert node.dn == "uni/infra"
        assert node.cls is infraInfra

    def test_aaep_dn(self, mock_niwaki: Niwaki) -> None:
        node = mock_niwaki.root.infra().aaep("niwaki")
        assert node.dn == "uni/infra/attentp-niwaki"
        assert node.cls is infraAttEntityP

    def test_phys_dom_dn(self, mock_niwaki: Niwaki) -> None:
        node = mock_niwaki.root.phys_dom("niwaki")
        assert node.dn == "uni/phys-niwaki"
        assert node.cls is physDomP

    def test_unknown_attr_raises_attribute_error(self, mock_niwaki: Niwaki) -> None:
        with pytest.raises(AttributeError, match="no child accessor"):
            _ = mock_niwaki.root.tenant("prod").nonexistent_thing  # type: ignore[attr-defined]

    def test_dunder_raises_attribute_error(self, mock_niwaki: Niwaki) -> None:
        with pytest.raises(AttributeError):
            _ = mock_niwaki.root.__nonexistent__  # type: ignore[attr-defined]


# ── Niwaki root proxy ─────────────────────────────────────────────────────────


class TestNiwakiProxy:
    def test_aci_tenant_shortcut(self, mock_niwaki: Niwaki) -> None:
        """aci.tenant("x") == aci.root.tenant("x")."""
        via_proxy = mock_niwaki.tenant("prod")  # type: ignore[attr-defined]
        via_root = mock_niwaki.root.tenant("prod")
        assert via_proxy.dn == via_root.dn
        assert via_proxy.cls is via_root.cls

    def test_aci_unknown_raises(self, mock_niwaki: Niwaki) -> None:
        with pytest.raises(AttributeError):
            _ = mock_niwaki.nonexistent  # type: ignore[attr-defined]


# ── Rs singleton navigators ───────────────────────────────────────────────────


class TestRsSingletonNavigator:
    """NiwakiNode.__getattr__ Rs-singleton branch: optional positional target_name."""

    @pytest.fixture()
    def epg(self, mock_niwaki: Niwaki) -> object:
        return mock_niwaki.root.tenant("prod").app("myapp").epg("frontend")

    @pytest.fixture()
    def bd(self, mock_niwaki: Niwaki) -> object:
        return mock_niwaki.root.tenant("prod").bd("web")

    def test_rs_singleton_navigates_by_class(self, epg: object) -> None:
        """Read-side Rs navigation — the singleton RN needs no target name."""
        node = epg.bd_binding()  # type: ignore[union-attr]
        assert node.cls is fvRsBd
        assert node.dn == "uni/tn-prod/ap-myapp/epg-frontend/rsbd"

    def test_rs_custom_qos_singleton(self, epg: object) -> None:
        node = epg.custom_qos_policy()  # type: ignore[union-attr]
        assert node.cls is fvRsCustQosPol

    def test_rs_vrf_binding_on_bd(self, bd: object) -> None:
        node = bd.vrf_binding()  # type: ignore[union-attr]
        assert node.cls is fvRsCtx
        assert node.dn == "uni/tn-prod/BD-web/rsctx"
