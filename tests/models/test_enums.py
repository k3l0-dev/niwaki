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


class TestColourSynonyms:
    """Two spellings of one colour must not read back as a change.

    ``pol:Color`` and ``health:ColorT`` list ``cyan``/``aqua`` and
    ``magenta``/``fuchsia`` against the same numeric code — the X11 pairs.  The
    APIC accepts either on write and answers with one of them, so keeping both
    as members made a design permanently disagree with the fabric it had just
    configured: declare ``magenta``, read back ``fuchsia``, and every later
    ``mode="plan"`` reports a change that is not there.
    """

    def test_only_the_stored_spelling_is_canonical(self) -> None:
        """Iteration yields the spelling the fabric holds, and only that one."""
        from niwaki.models._generated.enums.PolColor import PolColor

        names = {member.name for member in PolColor}
        assert "FUCHSIA" in names
        assert "AQUA" in names
        assert "MAGENTA" not in names
        assert "CYAN" not in names

    def test_the_other_spelling_stays_reachable_as_a_member(self) -> None:
        """The name a 1.7.0 user could already have written must keep resolving.

        Collapsing the pair fixed the plan drift but deleted two public names.
        Python's own enum aliasing gives both at once: the alias member *is* the
        canonical member, so it carries the stored value and writes it to the
        wire, while attribute access never breaks.
        """
        from niwaki.models._generated.enums.PolColor import PolColor

        assert PolColor.MAGENTA is PolColor.FUCHSIA
        assert PolColor.CYAN is PolColor.AQUA
        assert PolColor.MAGENTA.value == "fuchsia"  # what reaches the APIC
        assert PolColor.CYAN.value == "aqua"

    def test_the_alias_member_is_reachable_from_the_public_path(self) -> None:
        """Not just the private one: this is the import the guide teaches."""
        from niwaki.models.enums.HealthColorT import HealthColorT

        assert HealthColorT.MAGENTA is HealthColorT.FUCHSIA
        assert HealthColorT.CYAN is HealthColorT.AQUA

    def test_the_discarded_spelling_still_coerces(self) -> None:
        """Writing ``magenta`` keeps working — it simply lands on what is stored."""
        from niwaki.models._generated.enums.PolColor import PolColor

        assert PolColor("magenta") is PolColor.FUCHSIA
        assert PolColor("cyan") is PolColor.AQUA

    def test_numeric_aliases_still_resolve(self) -> None:
        """The synonym entry must not displace the hex alias for the same value."""
        from niwaki.models._generated.enums.PolColor import PolColor

        assert PolColor("0xFF00FF") is PolColor.FUCHSIA
        assert PolColor("0x00FFFF") is PolColor.AQUA

    def test_unrelated_colours_are_untouched(self) -> None:
        from niwaki.models._generated.enums.PolColor import PolColor

        assert PolColor("chartreuse").value == "chartreuse"
        assert PolColor("dark-magenta").value == "dark-magenta"

    def test_health_colour_collapses_the_same_way(self) -> None:
        from niwaki.models._generated.enums.HealthColorT import HealthColorT

        assert HealthColorT("magenta") is HealthColorT.FUCHSIA
        assert HealthColorT("cyan") is HealthColorT.AQUA

    def test_both_spellings_reach_the_wire_as_one(self) -> None:
        """The property that made this visible: a contract label's colour tag."""
        import json

        from niwaki.design import tenant

        payloads = []
        for spelling in ("magenta", "fuchsia"):
            design = tenant("t")
            design.app("a").epg("e").consumer_label("web", tag=spelling)
            payloads.append(json.dumps(design.to_payload()))

        assert payloads[0] == payloads[1]
        assert "fuchsia" in payloads[0]
        assert "magenta" not in payloads[0]
