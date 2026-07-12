"""Tests for generated StrEnum classes in niwaki.models._generated.enums._all.

Covers:
- Canonical localName values are accepted
- UPPER_SNAKE_CASE members map to the correct string value
- _missing_ classmethod resolves numeric (hex/decimal) aliases
- Invalid values raise ValueError
- StrEnum interoperability with plain strings and Pydantic models
"""

from __future__ import annotations

import pytest

from niwaki.models._generated.enums._all import L2EtherType, L2MultiDstPktAct, L2UnkMacUcastAct
from niwaki.models._generated.vz.vzEntry import vzEntry


class TestL2EtherType:
    """L2EtherType covers the hex-alias pattern (0x806, 0x0800, etc.)."""

    def test_canonical_values_accepted(self) -> None:
        assert L2EtherType("arp") is L2EtherType.ARP
        assert L2EtherType("ipv4") is L2EtherType.IPV4
        assert L2EtherType("ipv6") is L2EtherType.IPV6
        assert L2EtherType("unspecified") is L2EtherType.UNSPECIFIED

    def test_member_value_is_local_name(self) -> None:
        assert L2EtherType.ARP == "arp"
        assert L2EtherType.IPV4 == "ipv4"
        assert L2EtherType.UNSPECIFIED == "unspecified"

    def test_hex_alias_arp(self) -> None:
        assert L2EtherType("0x806") is L2EtherType.ARP

    def test_hex_alias_ipv4(self) -> None:
        assert L2EtherType("0x0800") is L2EtherType.IPV4

    def test_hex_alias_ipv6(self) -> None:
        assert L2EtherType("0x86DD") is L2EtherType.IPV6

    def test_decimal_alias_unspecified(self) -> None:
        assert L2EtherType("0") is L2EtherType.UNSPECIFIED

    def test_invalid_value_raises(self) -> None:
        with pytest.raises(ValueError):
            L2EtherType("not_an_ether_type")

    def test_is_str(self) -> None:
        assert isinstance(L2EtherType.ARP, str)
        assert L2EtherType.ARP == "arp"


class TestL2MultiDstPktAct:
    """L2MultiDstPktAct covers the decimal-alias pattern (0, 1, 2)."""

    def test_canonical_values(self) -> None:
        assert L2MultiDstPktAct("bd-flood") is L2MultiDstPktAct.BD_FLOOD
        assert L2MultiDstPktAct("drop") is L2MultiDstPktAct.DROP
        assert L2MultiDstPktAct("encap-flood") is L2MultiDstPktAct.ENCAP_FLOOD

    def test_decimal_alias_bd_flood(self) -> None:
        assert L2MultiDstPktAct("0") is L2MultiDstPktAct.BD_FLOOD

    def test_decimal_alias_encap_flood(self) -> None:
        assert L2MultiDstPktAct("1") is L2MultiDstPktAct.ENCAP_FLOOD

    def test_decimal_alias_drop(self) -> None:
        assert L2MultiDstPktAct("2") is L2MultiDstPktAct.DROP

    def test_member_value_has_dash(self) -> None:
        assert L2MultiDstPktAct.BD_FLOOD == "bd-flood"
        assert L2MultiDstPktAct.ENCAP_FLOOD == "encap-flood"


class TestL2UnkMacUcastAct:
    def test_proxy(self) -> None:
        assert L2UnkMacUcastAct("proxy") is L2UnkMacUcastAct.PROXY

    def test_flood(self) -> None:
        assert L2UnkMacUcastAct("flood") is L2UnkMacUcastAct.FLOOD


class TestVzEntryPydanticIntegration:
    """StrEnum fields in generated Pydantic models accept both strings and enum members."""

    def test_default_is_unspecified(self) -> None:
        entry = vzEntry(name="e")
        assert entry.ethernet_type is L2EtherType.UNSPECIFIED

    def test_accepts_localname_string(self) -> None:
        entry = vzEntry(name="e", ethernet_type="ipv4")  # type: ignore[reportArgumentType]
        assert entry.ethernet_type is L2EtherType.IPV4

    def test_accepts_enum_member(self) -> None:
        entry = vzEntry(name="e", ethernet_type=L2EtherType.ARP)
        assert entry.ethernet_type is L2EtherType.ARP

    def test_accepts_hex_alias_via_missing(self) -> None:
        entry = vzEntry(name="e", ethernet_type="0x0800")  # type: ignore[arg-type]
        assert entry.ethernet_type is L2EtherType.IPV4

    def test_serialises_localname_to_apic(self) -> None:
        entry = vzEntry(name="e", ethernet_type="ipv4")  # type: ignore[reportArgumentType]
        apic = entry.to_apic()
        assert apic["vzEntry"]["attributes"]["etherT"] == "ipv4"

    def test_invalid_etherT_raises(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            vzEntry(name="e", ethernet_type="not_valid")  # type: ignore[arg-type]
