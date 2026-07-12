"""Tests for the five generated target classes.

Covers per class: naming constraints, enum defaults and validation,
bool defaults, RN format, to_apic surgical serialisation, from_apic
roundtrip.  Error cases ensure client-side validation fires before any
APIC call.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from niwaki.models._generated.fv.fvAEPg import fvAEPg
from niwaki.models._generated.fv.fvAp import fvAp
from niwaki.models._generated.fv.fvBD import fvBD
from niwaki.models._generated.fv.fvCtx import fvCtx
from niwaki.models._generated.fv.fvTenant import fvTenant
from niwaki.models.base import ManagedObject

# ── fvTenant ──────────────────────────────────────────────────────────────────


class TestFvTenant:
    def test_requires_name(self) -> None:
        with pytest.raises(ValidationError):
            fvTenant()  # type: ignore[reportCallIssue]

    def test_name_empty_string_rejected(self) -> None:
        with pytest.raises(ValidationError):
            fvTenant(name="")

    def test_name_max_63_chars(self) -> None:
        with pytest.raises(ValidationError):
            fvTenant(name="t" * 64)

    def test_name_at_limit_accepted(self) -> None:
        t = fvTenant(name="t" * 63)
        assert len(t.name) == 63

    def test_name_invalid_char_rejected(self) -> None:
        with pytest.raises(ValidationError):
            fvTenant(name="invalid name")  # space not allowed

    def test_name_valid_chars(self) -> None:
        fvTenant(name="prod-tenant_1.2")

    def test_rn_format(self) -> None:
        t = fvTenant(name="prod")
        assert t.rn == "tn-prod"

    def test_to_apic_only_sends_name(self) -> None:
        t = fvTenant(name="prod")
        attrs = t.to_apic()["fvTenant"]["attributes"]
        assert attrs == {"name": "prod"}

    def test_to_apic_includes_explicitly_set_descr(self) -> None:
        t = fvTenant(name="prod", description="Production tenant")
        attrs = t.to_apic()["fvTenant"]["attributes"]
        assert attrs["descr"] == "Production tenant"

    def test_descr_max_128_chars(self) -> None:
        with pytest.raises(ValidationError):
            fvTenant(name="x", description="d" * 129)

    def test_from_apic_roundtrip(self) -> None:
        raw = {"fvTenant": {"attributes": {"name": "prod", "descr": "test"}}}
        obj = ManagedObject.from_apic(raw)
        assert isinstance(obj, fvTenant)
        assert obj.name == "prod"
        assert obj.description == "test"


# ── fvCtx (VRF) ───────────────────────────────────────────────────────────────


class TestFvCtx:
    def test_requires_name(self) -> None:
        with pytest.raises(ValidationError):
            fvCtx()  # type: ignore[reportCallIssue]

    def test_name_empty_string_rejected(self) -> None:
        with pytest.raises(ValidationError):
            fvCtx(name="")

    def test_name_max_64_chars(self) -> None:
        with pytest.raises(ValidationError):
            fvCtx(name="v" * 65)

    def test_name_invalid_char_rejected(self) -> None:
        with pytest.raises(ValidationError):
            fvCtx(name="my vrf")  # space not allowed

    def test_rn_format(self) -> None:
        v = fvCtx(name="main")
        assert v.rn == "ctx-main"

    def test_default_pcEnfPref_is_enforced(self) -> None:
        v = fvCtx(name="main")
        assert v.policy_control_enforcement == "enforced"

    def test_default_pcEnfDir_is_ingress(self) -> None:
        v = fvCtx(name="main")
        assert v.policy_enforcement_direction == "ingress"

    def test_default_knwMcastAct_is_permit(self) -> None:
        v = fvCtx(name="main")
        assert v.known_multicast_action == "permit"

    def test_default_ipDataPlaneLearning_is_enabled(self) -> None:
        v = fvCtx(name="main")
        assert v.data_plane_learning == "enabled"

    def test_default_bdEnforcedEnable_is_false(self) -> None:
        v = fvCtx(name="main")
        assert v.bd_enforcement_status is False

    def test_invalid_pcEnfPref_rejected(self) -> None:
        with pytest.raises(ValidationError):
            fvCtx(name="main", policy_control_enforcement="invalid")  # type: ignore[reportArgumentType]

    def test_valid_pcEnfPref_values(self) -> None:
        fvCtx(name="main", policy_control_enforcement="enforced")  # type: ignore[reportArgumentType]
        fvCtx(name="main", policy_control_enforcement="unenforced")  # type: ignore[reportArgumentType]

    def test_valid_pcEnfDir_values(self) -> None:
        fvCtx(name="main", policy_enforcement_direction="ingress")  # type: ignore[reportArgumentType]
        fvCtx(name="main", policy_enforcement_direction="egress")  # type: ignore[reportArgumentType]
        fvCtx(name="main", policy_enforcement_direction="mixed")  # type: ignore[reportArgumentType]

    def test_to_apic_surgical(self) -> None:
        v = fvCtx(name="main", policy_control_enforcement="unenforced")  # type: ignore[reportArgumentType]
        attrs = v.to_apic()["fvCtx"]["attributes"]
        assert attrs["name"] == "main"
        assert attrs["pcEnfPref"] == "unenforced"
        assert "pcEnfDir" not in attrs  # not explicitly set
        assert "knwMcastAct" not in attrs

    def test_from_apic_roundtrip(self) -> None:
        raw = {"fvCtx": {"attributes": {"name": "main", "pcEnfPref": "unenforced"}}}
        obj = ManagedObject.from_apic(raw)
        assert isinstance(obj, fvCtx)
        assert obj.name == "main"


# ── fvBD (Bridge Domain) ──────────────────────────────────────────────────────
# Core smoke tests are in test_base.py::TestGeneratedFvBD.
# Here we add fvBD-specific field coverage.


class TestFvBD:
    def test_requires_name(self) -> None:
        with pytest.raises(ValidationError):
            fvBD()  # type: ignore[reportCallIssue]

    def test_name_empty_string_rejected(self) -> None:
        with pytest.raises(ValidationError):
            fvBD(name="")

    def test_name_max_64_chars(self) -> None:
        with pytest.raises(ValidationError):
            fvBD(name="b" * 65)

    def test_rn_format(self) -> None:
        bd = fvBD(name="web")
        assert bd.rn == "BD-web"

    def test_defaults(self) -> None:
        bd = fvBD(name="web")
        assert bd.unicast_routing is True
        assert bd.arp_flooding is False
        assert bd.ip_learning is True
        assert bd.limit_ip_learning_to_bd_subnets_only is True
        assert bd.bd_rogue_mcast_arp_packet_drop is True
        assert bd.multi_destination_packet_action == "bd-flood"
        assert bd.unknown_mac_unicast_action == "proxy"
        assert bd.unknown_multicast_destination_action == "flood"
        assert bd.type == "regular"

    def test_invalid_multiDstPktAct_rejected(self) -> None:
        with pytest.raises(ValidationError):
            fvBD(name="web", multi_destination_packet_action="broadcast")  # type: ignore[reportArgumentType]

    def test_valid_multiDstPktAct_values(self) -> None:
        fvBD(name="web", multi_destination_packet_action="bd-flood")  # type: ignore[reportArgumentType]
        fvBD(name="web", multi_destination_packet_action="drop")  # type: ignore[reportArgumentType]
        fvBD(name="web", multi_destination_packet_action="encap-flood")  # type: ignore[reportArgumentType]

    def test_bool_serialized_as_string_in_to_apic(self) -> None:
        bd = fvBD(name="web", unicast_routing=False)
        attrs = bd.to_apic()["fvBD"]["attributes"]
        assert attrs["unicastRoute"] == "false"

    def test_name_invalid_char_rejected(self) -> None:
        with pytest.raises(ValidationError):
            fvBD(name="my bd")  # space not allowed

    def test_from_apic_roundtrip(self) -> None:
        raw = {
            "fvBD": {
                "attributes": {
                    "name": "web",
                    "unicastRoute": "yes",
                    "multiDstPktAct": "bd-flood",
                }
            }
        }
        obj = ManagedObject.from_apic(raw)
        assert isinstance(obj, fvBD)
        assert obj.name == "web"


# ── fvAp (Application Profile) ────────────────────────────────────────────────


class TestFvAp:
    def test_requires_name(self) -> None:
        with pytest.raises(ValidationError):
            fvAp()  # type: ignore[reportCallIssue]

    def test_name_empty_string_rejected(self) -> None:
        with pytest.raises(ValidationError):
            fvAp(name="")

    def test_name_max_64_chars(self) -> None:
        with pytest.raises(ValidationError):
            fvAp(name="a" * 65)

    def test_name_accepts_colon(self) -> None:
        # fvAp pattern allows ':' unlike fvTenant
        fvAp(name="app:v1")

    def test_rn_format(self) -> None:
        ap = fvAp(name="shop")
        assert ap.rn == "ap-shop"

    def test_default_prio_is_unspecified(self) -> None:
        ap = fvAp(name="shop")
        assert ap.priority == "unspecified"

    def test_invalid_prio_rejected(self) -> None:
        with pytest.raises(ValidationError):
            fvAp(name="shop", priority="high")  # type: ignore[reportArgumentType]

    def test_valid_prio_values(self) -> None:
        for p in ("level1", "level2", "level3", "level4", "level5", "level6", "unspecified"):
            fvAp(name="shop", priority=p)  # type: ignore[reportArgumentType]

    def test_to_apic_sends_only_name_by_default(self) -> None:
        ap = fvAp(name="shop")
        attrs = ap.to_apic()["fvAp"]["attributes"]
        assert attrs == {"name": "shop"}

    def test_to_apic_includes_prio_when_set(self) -> None:
        ap = fvAp(name="shop", priority="level1")  # type: ignore[reportArgumentType]
        attrs = ap.to_apic()["fvAp"]["attributes"]
        assert attrs["prio"] == "level1"

    def test_from_apic_roundtrip(self) -> None:
        raw = {"fvAp": {"attributes": {"name": "shop", "prio": "level2"}}}
        obj = ManagedObject.from_apic(raw)
        assert isinstance(obj, fvAp)
        assert obj.priority == "level2"


# ── fvAEPg (Endpoint Group) ────────────────────────────────────────────────────


class TestFvAEPg:
    def test_requires_name(self) -> None:
        with pytest.raises(ValidationError):
            fvAEPg()  # type: ignore[reportCallIssue]

    def test_name_empty_string_rejected(self) -> None:
        with pytest.raises(ValidationError):
            fvAEPg(name="")

    def test_name_max_64_chars(self) -> None:
        with pytest.raises(ValidationError):
            fvAEPg(name="e" * 65)

    def test_rn_format(self) -> None:
        epg = fvAEPg(name="web")
        assert epg.rn == "epg-web"

    def test_defaults(self) -> None:
        epg = fvAEPg(name="web")
        assert epg.flood_on_encap == "disabled"
        assert epg.policy_control_enforcement == "unenforced"
        assert epg.provider_label_match_criteria == "AtleastOne"
        assert epg.preferred_group_member == "exclude"
        assert epg.qos_class == "unspecified"
        assert epg.epg_with_multisite_mcast_source is False
        assert epg.attribute_based_epg is False
        assert epg.shutdown is False

    def test_invalid_floodOnEncap_rejected(self) -> None:
        with pytest.raises(ValidationError):
            fvAEPg(name="web", flood_on_encap="yes")  # type: ignore[reportArgumentType]

    def test_valid_floodOnEncap_values(self) -> None:
        fvAEPg(name="web", flood_on_encap="disabled")  # type: ignore[reportArgumentType]
        fvAEPg(name="web", flood_on_encap="enabled")  # type: ignore[reportArgumentType]

    def test_invalid_pcEnfPref_rejected(self) -> None:
        with pytest.raises(ValidationError):
            fvAEPg(name="web", policy_control_enforcement="strict")  # type: ignore[reportArgumentType]

    def test_valid_matchT_values(self) -> None:
        for v in ("All", "AtleastOne", "AtmostOne", "None"):
            fvAEPg(name="web", provider_label_match_criteria=v)  # type: ignore[reportArgumentType]

    def test_shutdown_bool_serialized(self) -> None:
        epg = fvAEPg(name="web", shutdown=True)
        attrs = epg.to_apic()["fvAEPg"]["attributes"]
        assert attrs["shutdown"] == "true"

    def test_to_apic_surgical(self) -> None:
        epg = fvAEPg(name="web", flood_on_encap="enabled")  # type: ignore[reportArgumentType]
        attrs = epg.to_apic()["fvAEPg"]["attributes"]
        assert attrs["name"] == "web"
        assert attrs["floodOnEncap"] == "enabled"
        assert "pcEnfPref" not in attrs
        assert "shutdown" not in attrs

    def test_from_apic_roundtrip(self) -> None:
        raw = {
            "fvAEPg": {
                "attributes": {
                    "name": "web",
                    "floodOnEncap": "enabled",
                    "shutdown": "no",
                }
            }
        }
        obj = ManagedObject.from_apic(raw)
        assert isinstance(obj, fvAEPg)
        assert obj.name == "web"
