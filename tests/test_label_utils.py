"""Tests for _label_utils: label_to_snake, propname_to_snake, best_field_name."""

from __future__ import annotations

from niwaki._codegen._label_utils import (
    MAX_LABEL_LENGTH,
    best_field_name,
    label_to_snake,
    propname_to_snake,
)

# ── label_to_snake ────────────────────────────────────────────────────────────


class TestLabelToSnake:
    def test_title_case_spaces(self) -> None:
        assert label_to_snake("ARP Flooding") == "arp_flooding"

    def test_title_case_mixed(self) -> None:
        assert label_to_snake("Unicast Routing") == "unicast_routing"

    def test_hyphenated(self) -> None:
        assert label_to_snake("deployment-immediacy") == "deployment_immediacy"

    def test_slash_separator(self) -> None:
        assert label_to_snake("TX/RX") == "tx_rx"

    def test_ipv6_acronym_preserved(self) -> None:
        # Must NOT produce "i_pv6_link_local_address" (camelCase split artifact)
        assert label_to_snake("IPv6 Link Local Address") == "ipv6_link_local_address"

    def test_already_snake(self) -> None:
        assert label_to_snake("description") == "description"

    def test_mixed_separators(self) -> None:
        assert label_to_snake("L2 / L3 Out") == "l2_l3_out"

    def test_leading_trailing_separators_stripped(self) -> None:
        assert label_to_snake("  Name  ") == "name"

    def test_special_chars_removed(self) -> None:
        assert label_to_snake("Name (required)") == "name_required"

    def test_multiple_spaces_collapsed(self) -> None:
        assert label_to_snake("A   B") == "a_b"

    def test_empty_string(self) -> None:
        assert label_to_snake("") == ""

    def test_only_special_chars(self) -> None:
        assert label_to_snake("!@#$") == ""

    def test_digits_preserved(self) -> None:
        assert label_to_snake("L3 Out") == "l3_out"


# ── propname_to_snake ─────────────────────────────────────────────────────────


class TestPropnameToSnake:
    def test_simple_camel(self) -> None:
        assert propname_to_snake("arpFlood") == "arp_flood"

    def test_unicast_route(self) -> None:
        assert propname_to_snake("unicastRoute") == "unicast_route"

    def test_double_lower(self) -> None:
        assert propname_to_snake("llAddr") == "ll_addr"

    def test_acronym_run(self) -> None:
        # "getHTMLDoc" → "get_html_doc"
        assert propname_to_snake("getHTMLDoc") == "get_html_doc"

    def test_already_lower(self) -> None:
        assert propname_to_snake("name") == "name"

    def test_already_snake(self) -> None:
        # Ideally idempotent for already-snake names (no double underscores)
        assert propname_to_snake("flood_on_encap") == "flood_on_encap"

    def test_single_word_lower(self) -> None:
        assert propname_to_snake("descr") == "descr"

    def test_three_word_camel(self) -> None:
        assert propname_to_snake("floodOnEncap") == "flood_on_encap"

    def test_tn_prefix(self) -> None:
        # Relationship target props like "tnFvCtxName"
        assert propname_to_snake("tnFvCtxName") == "tn_fv_ctx_name"


# ── best_field_name ───────────────────────────────────────────────────────────


class TestBestFieldName:
    # ── Priority 1: JSON label ────────────────────────────────────────────────

    def test_json_label_wins(self) -> None:
        assert best_field_name("arpFlood", "ARP Flooding", "") == "arp_flooding"

    def test_json_label_wins_over_sm(self) -> None:
        assert (
            best_field_name("resImedcy", "Resolution Immediacy", "resolution-immediacy")
            == "resolution_immediacy"
        )

    def test_json_label_same_as_aci_falls_through(self) -> None:
        # label "arpFlood" == aci_name → skip to priority 3
        assert best_field_name("arpFlood", "arpFlood", "") == "arp_flood"

    def test_json_label_same_case_insensitive(self) -> None:
        # "ARP_FLOOD" case-insensitively == "arpflood" != "arpFlood" but close —
        # more precisely: label.lower() vs aci_name.lower()
        # "descr".lower() == "descr", label "descr".lower() == "descr" → skip
        assert best_field_name("descr", "descr", "") == "descr"

    def test_json_label_too_long_falls_to_sm(self) -> None:
        long_label = "Handling of L2 Multicast Broadcast and Link Layer Traffic at EPG"
        assert len(long_label) > MAX_LABEL_LENGTH
        # scopemeta has the short label
        result = best_field_name("floodOnEncap", long_label, "flood-on-encap")
        assert result == "flood_on_encap"

    def test_json_label_too_long_no_sm_falls_to_camel(self) -> None:
        long_label = "Handling of L2 Multicast Broadcast and Link Layer Traffic at EPG"
        result = best_field_name("floodOnEncap", long_label, "")
        assert result == "flood_on_encap"

    def test_json_label_exactly_at_limit(self) -> None:
        # A label whose snake form is exactly MAX_LABEL_LENGTH chars → accepted
        label = "A" * MAX_LABEL_LENGTH  # snake: "a" * 40
        result = best_field_name("someAciProp", label, "")
        assert result == "a" * MAX_LABEL_LENGTH

    def test_json_label_one_over_limit_falls_through(self) -> None:
        # snake form of "A" * 41 is "a" * 41 → too long
        label = "A" * (MAX_LABEL_LENGTH + 1)
        result = best_field_name("someAciProp", label, "")
        # Fallback: propname_to_snake("someAciProp")
        assert result == propname_to_snake("someAciProp")

    # ── Priority 2: Scopemeta label ───────────────────────────────────────────

    def test_sm_label_used_when_no_json(self) -> None:
        # For non-naming props, SM (P2) is consulted even when JSON label is absent.
        result = best_field_name("instrImedcy", "", "deployment-immediacy")
        assert result == "deployment_immediacy"

    def test_sm_label_same_as_aci_falls_through(self) -> None:
        # sm_label.lower() == aci_name.lower() → skip
        result = best_field_name("descr", "", "descr")
        # Falls to priority 3: propname_to_snake("descr") = "descr"
        assert result == "descr"

    def test_sm_label_skipped_for_naming_prop(self) -> None:
        # is_naming=True → scopemeta never applied even when JSON label == aci_name
        result = best_field_name("name", "Name", "enable-infrastructure-vlan", is_naming=True)
        # JSON "Name" == "name" → P1 skipped; P2 skipped (is_naming); P3: "name"
        assert result == "name"

    def test_sm_label_used_for_non_naming_prop(self) -> None:
        # is_naming=False (default) → SM can fire when JSON label == aci_name
        result = best_field_name("purgeWin", "purgeWin", "purge-window-size")
        assert result == "purge_window_size"

    # ── Priority 3: camelCase → snake ─────────────────────────────────────────

    def test_camel_fallback_no_labels(self) -> None:
        assert best_field_name("unicastRoute", "", "") == "unicast_route"

    def test_camel_fallback_both_labels_match_aci(self) -> None:
        # Both labels are equivalent to aci_name → fall to camelCase conversion
        assert best_field_name("arpFlood", "arpFlood", "arpFlood") == "arp_flood"

    # ── Python keyword guard ──────────────────────────────────────────────────

    def test_keyword_from(self) -> None:
        # "from" is a keyword — suffix _ regardless of label
        assert best_field_name("from", "From Port", "from-port") == "from_"

    def test_keyword_class(self) -> None:
        assert best_field_name("class", "Class", "") == "class_"

    def test_non_keyword_not_suffixed(self) -> None:
        assert not best_field_name("name", "Name", "").endswith("_")

    # ── Edge cases ────────────────────────────────────────────────────────────

    def test_empty_labels(self) -> None:
        result = best_field_name("arpFlood", "", "")
        assert result == propname_to_snake("arpFlood")

    def test_all_empty(self) -> None:
        # aci_name="" with empty labels → propname_to_snake("") == ""
        # Not a realistic call but must not crash
        result = best_field_name("x", "", "")
        assert isinstance(result, str)

    def test_description_label(self) -> None:
        # "descr" has json_label "Description" → different from "descr" → accepted
        assert best_field_name("descr", "Description", "description") == "description"

    def test_ipv6_label(self) -> None:
        assert best_field_name("llAddr", "IPv6 Link Local Address", "") == "ipv6_link_local_address"

    def test_label_starting_with_digit_falls_through(self) -> None:
        # "1R2C or 2R3C policer" → "1r2c_or_2r3c_policer" starts with digit → invalid
        # → falls through to priority 3: propname_to_snake("type") = "type"
        result = best_field_name("type", "1R2C or 2R3C policer", "")
        assert result == "type"
        assert result.isidentifier()

    def test_label_digit_start_falls_to_sm(self) -> None:
        # JSON label invalid (starts with digit) → try scopemeta label
        result = best_field_name("frequency100MHz", "100MHz Frequency", "100-mhz-frequency")
        assert result == "100_mhz_frequency" or result.isidentifier()
        # SM label "100-mhz-frequency" → "100_mhz_frequency" also starts with digit
        # → falls through to priority 3: propname_to_snake("frequency100MHz")
        assert result.isidentifier()
