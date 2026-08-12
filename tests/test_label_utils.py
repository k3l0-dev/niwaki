"""Tests for _label_utils: label_to_snake, propname_to_snake, best_field_name."""

from __future__ import annotations

import pytest

from niwaki._schema.naming import (
    FIELD_NAME_OVERRIDES,
    LABEL_CORRECTIONS,
    LABEL_MARKERS,
    MAX_LABEL_LENGTH,
    MAX_LABEL_WORDS,
    NAV_NAME_OVERRIDES,
    best_field_name,
    classname_to_snake,
    label_to_snake,
    propname_to_snake,
    resolve_py_names,
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

    def test_json_label_too_long_falls_through(self) -> None:
        long_label = "Handling of L2 Multicast Broadcast and Link Layer Traffic at EPG"
        assert len(long_label) > MAX_LABEL_LENGTH
        # The sm candidate "flood-on-encap" carries the marker "on" and is
        # gated too — the identical final name comes from the wire spelling.
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

    def test_single_letter_wire_name_passes_through(self) -> None:
        result = best_field_name("x", "", "")
        assert result == "x"

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

    def test_label_digit_start_falls_to_wire(self) -> None:
        # JSON label invalid (starts with digit) → scopemeta candidate also
        # starts with a digit → priority 3: propname_to_snake of the wire name.
        result = best_field_name("frequency100MHz", "100MHz Frequency", "100-mhz-frequency")
        assert result == propname_to_snake("frequency100MHz")
        assert result.isidentifier()


# ── classname_to_snake ────────────────────────────────────────────────────────


class TestClassnameToSnake:
    def test_simple_pascal(self) -> None:
        assert classname_to_snake("DevFolder") == "dev_folder"

    def test_leading_acronym_stays_one_token(self) -> None:
        assert classname_to_snake("EPg") == "epg"

    def test_inner_acronym_stays_attached(self) -> None:
        assert classname_to_snake("ThrValueUByte") == "thr_value_ubyte"

    def test_relation_class_shape(self) -> None:
        assert classname_to_snake("RsSrcToVPortDef") == "rs_src_to_vport_def"

    def test_digit_boundary_splits(self) -> None:
        assert classname_to_snake("L3Out") == "l3_out"

    def test_single_word(self) -> None:
        assert classname_to_snake("Pol") == "pol"

    def test_already_lower(self) -> None:
        assert classname_to_snake("pol") == "pol"

    def test_empty(self) -> None:
        assert classname_to_snake("") == ""


class TestNavNameOverrides:
    def test_table_is_empty(self) -> None:
        # The historical typo entries moved to LABEL_CORRECTIONS (fixed in the
        # common funnel); the mechanism stays as an escape hatch only.
        assert NAV_NAME_OVERRIDES == {}

    def test_typo_names_now_derive_from_corrected_labels(self) -> None:
        # The exact names the old overrides pinned, now produced by derivation.
        assert label_to_snake("Catalog Maitenance Policy") == "catalog_maintenance_policy"
        assert label_to_snake("VMM Host Availibility Policy") == "vmm_host_availability_policy"


# ── Acceptance gate (word cap + grammar markers) ──────────────────────────────


class TestAcceptanceGate:
    # ── Word cap ──────────────────────────────────────────────────────────────

    def test_four_word_label_accepted(self) -> None:
        assert best_field_name("llAddr", "IPv6 Link Local Address", "") == "ipv6_link_local_address"

    def test_five_word_label_rejected_to_wire(self) -> None:
        # "Optimize Wan Bandwidth between sites" → 5 words → wire spelling.
        result = best_field_name("OptimizeWanBandwidth", "Optimize Wan Bandwidth between sites", "")
        assert result == "optimize_wan_bandwidth"

    def test_sentence_label_under_char_cap_rejected(self) -> None:
        # 39 chars — slipped under the historical char cap, gated by words now.
        label = "Indicate whether MPLS is enabled or not"
        assert len(label) <= MAX_LABEL_LENGTH
        assert best_field_name("mplsEnabled", label, "") == "mpls_enabled"

    def test_word_cap_boundary_is_exactly_max_words(self) -> None:
        accepted = " ".join(["Word"] * MAX_LABEL_WORDS)
        rejected = " ".join(["Word"] * (MAX_LABEL_WORDS + 1))
        assert best_field_name("someProp", accepted, "") == "word_" + "_".join(
            ["word"] * (MAX_LABEL_WORDS - 1)
        )
        assert best_field_name("someProp", rejected, "") == "some_prop"

    # ── Grammar markers ───────────────────────────────────────────────────────

    def test_marker_of_rejected_to_wire(self) -> None:
        # 4 words — passes the word cap, gated by "of"/"the".
        result = best_field_name("scope", "Visibility of the Subnet", "visibility-of-the-subnet")
        assert result == "scope"

    def test_marker_rejected_falls_to_scopemeta(self) -> None:
        # JSON label is prose; scopemeta has the operator jargon.
        result = best_field_name(
            "enforceRtctrl",
            "Enforce Route Control for Following Directions",
            "enforce-route-control",
        )
        assert result == "enforce_route_control"

    def test_scopemeta_candidate_gated_too(self) -> None:
        # Both label sources are sentences → wire spelling wins.
        result = best_field_name(
            "limitIpLearnToSubnets",
            "Limit IP learning to BD subnets only",
            "limit-ip-learn-to-subnets",
        )
        assert result == "limit_ip_learn_to_subnets"

    def test_wire_fallback_is_never_gated(self) -> None:
        # Wire names keep grammar words and any length — they are the truth.
        assert best_field_name("mcpPduPerVlan", "", "") == "mcp_pdu_per_vlan"
        assert (
            best_field_name("enableVrfValidationOspfArea", "", "")
            == "enable_vrf_validation_ospf_area"
        )

    def test_marker_in_any_position_rejects(self) -> None:
        assert best_field_name("joinType", "Join Type of Groups", "") == "join_type"
        assert best_field_name("latest", "Whether Latest", "") == "latest"

    # ── Python keywords ───────────────────────────────────────────────────────

    def test_keyword_label_rejected_to_wire(self) -> None:
        # Cisco labels a prop "Class" → the candidate `class` is a valid
        # identifier but a keyword: `mo.class` would be a SyntaxError.
        assert best_field_name("matchClass", "Class", "") == "match_class"
        assert best_field_name("importT", "Import", "") == "import_t"

    # ── Measured jargon exclusions (from/to/as/use are NOT markers) ───────────

    def test_from_to_labels_survive(self) -> None:
        assert best_field_name("from_", "From Node id", "") == "from_node_id"
        assert best_field_name("dFromPort", "Destination From Port", "") == "destination_from_port"
        assert best_field_name("ttl", "Time to Live", "") == "time_to_live"

    def test_as_labels_survive(self) -> None:
        assert best_field_name("privateASctrl", "Private AS Control", "") == "private_as_control"
        assert best_field_name("criteria", "AS Path Criteria", "") == "as_path_criteria"

    def test_use_labels_survive(self) -> None:
        result = best_field_name("useConfiguredSystemGIPo", "Use Configured System GIPo", "")
        assert result == "use_configured_system_gipo"

    def test_marker_set_is_lowercase_single_tokens(self) -> None:
        for marker in LABEL_MARKERS:
            assert marker == marker.lower() and "_" not in marker and marker

    def test_excluded_jargon_words_not_in_markers(self) -> None:
        assert {"from", "to", "as", "use", "used", "using"}.isdisjoint(LABEL_MARKERS)


# ── LABEL_CORRECTIONS (Cisco label typos) ─────────────────────────────────────


class TestLabelCorrections:
    def test_typo_tokens_corrected(self) -> None:
        assert label_to_snake("Catalog Maitenance Policy") == "catalog_maintenance_policy"
        assert label_to_snake("VMM Host Availibility Policy") == "vmm_host_availability_policy"

    def test_correction_reaches_field_names(self) -> None:
        # vmmDomP.hvAvailMonitor shipped the typo in its field name pre-2.0.
        result = best_field_name("hvAvailMonitor", "Enable Host availibility monitoring", "")
        assert result == "enable_host_availability_monitoring"

    def test_clean_labels_untouched(self) -> None:
        assert label_to_snake("Maintenance Policy") == "maintenance_policy"

    def test_correction_is_token_exact_not_substring(self) -> None:
        # A token merely containing a typo key must not be rewritten.
        assert label_to_snake("premaitenance") == "premaitenance"

    def test_keys_and_values_are_snake_tokens(self) -> None:
        for typo, fix in LABEL_CORRECTIONS.items():
            assert typo and typo == typo.lower() and "_" not in typo
            assert fix and fix == fix.lower() and "_" not in fix
            assert typo != fix


# ── FIELD_NAME_OVERRIDES (the true irreducibles) ──────────────────────────────


class TestFieldNameOverrides:
    def test_exactly_the_two_irreducibles(self) -> None:
        # 82 wire-spelling pins dissolved into the acceptance gate in 2.0;
        # growing this table back means the gate regressed.
        assert set(FIELD_NAME_OVERRIDES) == {
            ("fvSubnet", "preferred"),
            ("vzEntry", "applyToFrag"),
        }

    def test_preferred_override_applied(self) -> None:
        # "Preferred as primary subnet" is 4 words and "as" is (deliberately)
        # not a marker — only the override produces the operator word.
        props = {"preferred": {"label": "Preferred as primary subnet", "is_naming": False}}
        assert resolve_py_names(props, {}, "fvSubnet") == {"preferred": "preferred"}

    def test_apply_to_frag_override_applied(self) -> None:
        # Scopemeta says "allow-fragments"; the override keeps the wire-aligned
        # name so vzEntry and vzEntryPortZero stay consistent.
        props = {"applyToFrag": {"label": "Apply Rule for all Fragments", "is_naming": False}}
        sm = {"applyToFrag": "allow-fragments"}
        assert resolve_py_names(props, sm, "vzEntry") == {"applyToFrag": "apply_to_frag"}

    def test_override_scoped_to_its_class(self) -> None:
        # The same wire prop on another class derives normally.
        props = {"applyToFrag": {"label": "Apply Rule for all Fragments", "is_naming": False}}
        assert resolve_py_names(props, {}, "vzEntryPortZero") == {"applyToFrag": "apply_to_frag"}

    def test_override_does_not_leak_to_other_classes(self) -> None:
        # A class where derivation and the override value DIFFER proves the
        # (class, prop) scoping: xNotSubnet gets the derived sentence name,
        # not fvSubnet's pinned "preferred".
        props = {"preferred": {"label": "Preferred as primary subnet", "is_naming": False}}
        assert resolve_py_names(props, {}, "xNotSubnet") == {
            "preferred": "preferred_as_primary_subnet"
        }

    def test_override_losing_its_name_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Two overrides pinning the same name in one class: one must lose —
        # curation silently dropped is a build error, never a reassignment.
        monkeypatch.setitem(FIELD_NAME_OVERRIDES, ("xTest", "alpha"), "shared")
        monkeypatch.setitem(FIELD_NAME_OVERRIDES, ("xTest", "beta"), "shared")
        props = {
            "alpha": {"label": "", "is_naming": False},
            "beta": {"label": "", "is_naming": False},
        }
        with pytest.raises(ValueError, match="collided and lost"):
            resolve_py_names(props, {}, "xTest")


# ── resolve_py_names robustness under the gate ────────────────────────────────


class TestResolvePyNamesGate:
    def test_collision_keeps_both_props_distinct(self) -> None:
        # Two props resolving to the same accepted label: winner keeps the
        # label, loser falls back to its wire spelling — no silent drop.
        props = {
            "alpha": {"label": "Shared Label", "is_naming": False},
            "beta": {"label": "Shared Label", "is_naming": False},
        }
        resolved = resolve_py_names(props, {}, "xTest")
        assert set(resolved) == {"alpha", "beta"}
        assert len(set(resolved.values())) == 2

    def test_unresolvable_collision_raises(self) -> None:
        # Loser's wire fallback collides with the winner's name → ValueError.
        props = {
            "sharedLabel": {"label": "Shared Label", "is_naming": False},
            "shared_label": {"label": "Shared Label", "is_naming": False},
        }
        with pytest.raises(ValueError, match="collision"):
            resolve_py_names(props, {}, "xTest")

    def test_empty_props(self) -> None:
        assert resolve_py_names({}, {}, "xTest") == {}
