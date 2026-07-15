"""Curation consistency — vocabulary.yaml validated against generated data.

These tests are what keeps the hand-curated vocabulary honest when the APIC
schema (and therefore CHILD_MAP / REFERENCE_MAP / _contains) is regenerated.
"""

from __future__ import annotations

from typing import Any

import pytest

from niwaki.design._cursor import _load_class, _tables
from niwaki.design._sugar import apply_sugar
from niwaki.domain._child_map import CHILD_MAP, CLASS_PKG, REFERENCE_MAP, TARGET_SUBCLASSES

# Deliberate divergences from the facade jargon (CHILD_MAP names), documented
# here so the consistency test stays strict for everything else.  The design
# surface prefers short operator vocabulary; the facade jargon keeps longer
# read-navigation names derived from the schema labels.
_MAKER_RENAMES = {
    ("fvCtx", "pim"): "pim_ctx",
    ("infraInfra", "storm_control_policy"): "storm_control_interface_policy",
    ("fvnsVlanInstP", "range"): "ranges",
    ("fvnsVxlanInstP", "range"): "ranges",
    ("fvnsVsanInstP", "range"): "vsan_ranges",
    ("fvnsMcastAddrInstP", "range"): "abstraction_of_ip_address_block",
    # Wave 2 — policy groups, spine/FEX profiles, selectors
    ("infraFuncP", "breakout_group"): "leaf_breakout_port_group",
    ("infraFuncP", "fc_port_group"): "leaf_access_fc_port_policy_group",
    ("infraFuncP", "fc_port_channel"): "leaf_access_fc_pc_policy_group",
    ("infraFuncP", "fc_port_channel_override"): "leaf_access_fc_pc_policy_override_group",
    ("infraFuncP", "spine_access_group"): "spine_access_port_policy_group",
    ("infraFuncP", "leaf_switch_group"): "access_switch_policy_group",
    ("infraFuncP", "spine_switch_group"): "spine_switch_policy_group",
    ("infraFuncP", "access_card_group"): "access_card_policy_group",
    ("infraSpAccPortP", "port_selector"): "sub_port_selector",
    # Wave 3 — interface policies
    ("infraInfra", "lacp_member_policy"): "port_channel_member_policy",
    ("infraInfra", "fc_interface_policy"): "interface_fc_policy",
    ("infraInfra", "fc_fabric_policy"): "fibre_channel_fabric_level_policy",
    ("infraInfra", "macsec_interface_policy"): "macsec_access_interface_policy",
    ("infraInfra", "macsec"): "macsec_access_policy_container",
    ("infraInfra", "dot1x_node_authentication"): "802_1x_node_authentication_policy",
    ("infraInfra", "dot1x_port_authentication"): "802_1x_port_authentication_policy",
    ("l2PortAuthPol", "dot1x_port_authentication_config"): (
        "802_1x_port_authentication_configuration_policy"
    ),
    ("macsecPolCont", "parameters_policy"): "macsec_access_parameters_policy",
    ("macsecPolCont", "keychain_policy"): "macsec_keychain_policy",
    ("macsecKeyChainPol", "key_policy"): "macsec_key_policy",
    # Wave 4 — QoS + CoPP + prefilter
    ("infraInfra", "llfc_interface_policy"): "interface_link_level_flow_control_policy",
    ("infraInfra", "pfc_interface_policy"): "interface_priority_flow_control_policy",
    ("infraInfra", "copp_leaf_policy"): "copp_leaf_level_policy",
    ("infraInfra", "copp_spine_policy"): "copp_spine_level_policy",
    ("infraInfra", "copp_interface_policy"): "per_interface_per_protocol_copp_policy",
    ("infraInfra", "copp_prefilter_leaf_policy"): "copp_prefilter_leaf_level_policy",
    ("infraInfra", "copp_prefilter_spine_policy"): "copp_prefilter_spine_level_policy",
    ("coppIfPol", "protocol_class"): "per_interface_per_protocol_copp_policy",
    ("coppLeafProfile", "gen1_settings"): (
        "settings_of_burst_and_rate_for_all_protocols_on_leafs_of_first_generation"
    ),
    ("coppSpineProfile", "gen1_settings"): (
        "settings_of_burst_and_rate_for_all_protocols_on_spines_of_first_generation"
    ),
    ("iaclLeafProfile", "acl_entry"): "acl_entry_of_the_copp_prefilter",
    ("iaclSpineProfile", "acl_entry"): "acl_entry_of_the_copp_prefilter",
    # Wave 5 — fabric-wide / system
    ("infraInfra", "port_status_policy"): "port_status_infra_policy",
    ("infrazoneZone", "node_group"): "infrastructure_zone_node_group",
    ("infrazoneZone", "pod_group"): "zone_pod_group",
    ("mgmtGrp", "inband_zone"): "inb_managed_nodes_zone",
    ("mgmtGrp", "oob_zone"): "oob_managed_nodes_zone",
    # Wave 6 — observability & timing
    ("infraInfra", "netflow_vmm_exporter"): "vmm_external_collector_reachability",
    ("infraInfra", "ptp_domain"): ("user_configured_ptp_domain_will_be_associated_with_interface"),
    ("infraInfra", "ptp_profile_template"): "ptp_template_abstract",
    ("infraFuncP", "access_group"): "leaf_access_port_policy_group",
    ("infraSpineP", "spine_selector"): "switch_association",
    ("fabricInst", "datetime_policy"): "date_and_time_policy",
    ("fabricInst", "bgp_instance"): "bgp_route_reflector_policy",
    ("fabricInst", "syslog_group"): "syslog_monitoring_destination_group",
    ("fabricInst", "vpc_protection"): "virtual_port_channel_security_policy",
    ("datetimePol", "ntp_provider"): "providers",
    ("dnsProfile", "provider"): "dns_provider",
    ("dnsProfile", "domain"): "dns_domain",
    ("syslogGroup", "remote_destination"): "syslog_remote_destination",
    ("bgpInstPol", "autonomous_system"): "autonomous_system_profile",
    ("bgpInstPol", "route_reflector"): "bgp_route_reflector",
    ("bgpRRP", "node"): "route_reflector_node_policy_ep",
    ("fabricProtPol", "vpc_pair"): "vpc_explicit_protection_group",
    ("fabricExplicitGEp", "node"): "node_policy_end_point",
    ("ctrlrInst", "fabric_membership"): "fabric_membership_policy",
    # On a dhcp_relay_policy cursor, .provider() reads naturally — the dhcp_
    # prefix is redundant at that position (same call as dnsProfile.provider).
    ("dhcpRelayP", "provider"): "dhcp_provider",
    # Multicast (wave 2026-07-15): the DSL uses short PIM/IGMP operator names;
    # the facade keeps the long schema-derived read-navigation labels.
    ("fvCtx", "igmp"): "context_level_igmp_policy",
    ("fvCtx", "pim6"): "context_level_pim_ipv6_policy",
    ("pimCtxP", "asm_pattern"): "any_source_multicast_pattern_policy",
    ("pimCtxP", "ssm_pattern"): "source_specific_multicast_pattern_policy",
    ("pimCtxP", "auto_rp"): "auto_rp_pim_policy",
    ("pimCtxP", "bootstrap_rp"): "bootstrap_rp_pim_policy",
    ("pimCtxP", "fabric_rp"): "fabric_rp_pim_policy",
    ("pimCtxP", "static_rp"): "static_rp_pim_policy",
    ("pimCtxP", "stripe_winner"): "configured_stripe_winner_policy",
    ("pimCtxP", "inter_vrf"): "inter_vrf_pim_policy",
    ("pimCtxP", "resource"): "pim_resource_policy",
    ("pimIPV6CtxP", "asm_pattern"): "any_source_multicast_pattern_policy",
    ("pimIPV6CtxP", "ssm_pattern"): "source_specific_multicast_pattern_policy",
    ("pimIPV6CtxP", "auto_rp"): "auto_rp_pim_policy",
    ("pimIPV6CtxP", "bootstrap_rp"): "bootstrap_rp_pim_policy",
    ("pimIPV6CtxP", "fabric_rp"): "fabric_rp_pim_policy",
    ("pimIPV6CtxP", "static_rp"): "static_rp_pim_policy",
    ("pimIPV6CtxP", "inter_vrf"): "inter_vrf_pim_policy",
    ("pimIPV6CtxP", "resource"): "pim_resource_policy",
    ("pimASMPatPol", "register_traffic"): "pim_register_traffic_policy",
    ("pimASMPatPol", "sg_expiry"): "s_g_expiry_policy",
    ("pimASMPatPol", "shared_range"): "shared_tree_policy",
    ("pimSSMPatPol", "ssm_range"): "ssm_group_range_policy",
    ("pimAutoRPPol", "ma_filter"): "pim_ma_filter_policy",
    ("pimBSRPPol", "bsr_filter"): "pim_bs_filter_policy",
    ("pimFabricRPPol", "rp_entry"): "pim_static_rp_entry_policy",
    ("pimStaticRPPol", "rp_entry"): "pim_static_rp_entry_policy",
    ("pimStaticRPEntryPol", "group_range"): "pim_rp_group_range_policy",
    ("pimCSWPol", "entry"): "configured_stripe_winner_entry",
    ("pimInterVRFPol", "entry"): "pim_inter_vrf_entry_policy",
    ("pimBDP", "filter"): "pim_bd_filter_policy",
    ("pimBDFilterPol", "source_filter"): "source_routemap_for_the_bd_filter_policy",
    ("pimBDFilterPol", "destination_filter"): "destination_routemap_for_the_bd_filter_policy",
    ("pimIfPol", "neighbor_filter"): "neighbor_fiter_policy",
    ("pimIfPol", "inbound_jp_filter"): "pim_jpin_jp_inbound_filter_policy",
    ("pimIfPol", "outbound_jp_filter"): "pim_jpout_jp_inbound_filter_policy",
    ("igmpCtxP", "ssm_translate"): "context_level_ssm_translate_policy",
    ("igmpIfPol", "report"): "report_policy",
    ("igmpIfPol", "state_limit"): "state_limit_policy",
    ("igmpIfPol", "static_report"): "static_report_policy",
    ("fvRsPathAtt", "igmp_snoop_access_group"): "igmp_snooping_access_group_configuration",
    ("fvRsPathAtt", "igmp_snoop_static_group"): "igmp_snooping_static_membership_configuration",
    ("fvRsPathAtt", "mld_snoop_access_group"): "mld_snooping_access_group_configuration",
    ("fvRsPathAtt", "mld_snoop_static_group"): "mld_snooping_static_membership_configuration",
    # Route-control & leaking (wave 2026-07-15): Cisco route-map match/set clause
    # names on the DSL; the facade keeps the long schema-derived labels.
    ("rtctrlSubjP", "match_prefix"): "match_route_destination_rule",
    ("rtctrlSubjP", "match_community"): "match_community_term",
    ("rtctrlSubjP", "match_community_regex"): "match_rule_based_on_community_regular_expression",
    ("rtctrlSubjP", "match_as_path"): "match_rule_based_on_as_path_regular_expression",
    ("rtctrlMatchCommTerm", "factor"): "match_community_factor",
    ("rtctrlAttrP", "add_community"): "set_add_comm",
    ("rtctrlAttrP", "set_community"): "set_comm",
    ("rtctrlAttrP", "set_dampening"): "set_damp",
    ("rtctrlAttrP", "set_next_hop"): "set_nh",
    ("rtctrlAttrP", "set_next_hop_unchanged"): "nexthop_unchanged_action",
    ("rtctrlAttrP", "set_preference"): "set_pref",
    ("rtctrlAttrP", "set_redistribute_multipath"): "redistribute_multipath_action",
    ("rtctrlAttrP", "set_metric"): "set_rt_metric",
    ("rtctrlAttrP", "set_metric_type"): "set_rt_metric_type",
    ("rtctrlAttrP", "set_route_tag"): "set_tag",
    ("rtctrlSetASPath", "asn"): "as_number",
    ("fvCtx", "leak_routes"): "inter_vrf_leaked_routes_container",
    ("leakRoutes", "external_prefix"): "inter_vrf_leaked_external_prefix",
    ("leakRoutes", "internal_prefix"): "inter_vrf_leaked_internal_prefix",
    ("leakRoutes", "internal_subnet"): (
        "inter_vrf_leaked_subnet_epg_bd_in_case_of_apic_cloudsubnet_cloudcidr_in_case_of_capic"
    ),
    ("leakExternalPrefix", "leak_to"): "tenant_and_vrf_destination_for_inter_vrf_leaked_routes",
    ("leakInternalPrefix", "leak_to"): "tenant_and_vrf_destination_for_inter_vrf_leaked_routes",
    ("leakInternalSubnet", "leak_to"): "tenant_and_vrf_destination_for_inter_vrf_leaked_routes",
    ("ipRouteP", "next_hop"): "nexthop_profile",
    # L3Out remainder (wave 2026-07-15).
    ("l3extOut", "consumer_label"): "external_connectivity_consumer_label",
    ("l3extOut", "provider_label"): "external_connectivity_provider_label",
    ("l3extRsPathL3OutAtt", "forwarder_address"): "forwarding_ip_address",
    ("l3extRsPathL3OutAtt", "rogue_exception_mac"): "rogue_exception_mac_group_policy",
    ("l3extRsNodeL3OutAtt", "infra_node"): "infra_logical_node_profile",
    ("l3extRsNodeL3OutAtt", "loopback"): "loop_back_interface_profile",
    ("fvTenant", "rogue_exception_mac_group"): "abstract_rogue_exception_mac_group",
    ("l3extRogueExceptionMacGroup", "mac"): "abstract_rogue_exception_mac",
    # Security & VPN (wave 2026-07-15).
    ("fvTenant", "host_protection"): "host_protection_domain_policy",
    ("fvTenant", "isakmp_global"): "isakmp_cloud_router_level_common_policy",
    ("fvTenant", "ipsec_phase1"): "ipsec_isakmp_phase_1_policy",
    ("fvTenant", "ipsec_phase2"): "ipsec_isakmp_phase_2_policy",
    ("hostprotPol", "subject"): "host_protection_domain_subject",
    ("hostprotPol", "remote_ips"): "remote_ip_container",
    ("hostprotSubj", "rule"): "host_protection_domain_rule",
    ("hostprotRule", "remote_ip"): "remote_cidr",
    ("hostprotRule", "filter"): "remote_ip_filter_container",
    ("hostprotRemoteIpContainer", "remote_ip"): "remote_cidr",
    ("hostprotRemoteIp", "ep_label"): "endpoint_label",
    ("hostprotFilterContainer", "pod_filter"): "endpoint_label",
    ("fvRsPathAtt", "port_security"): "port_security_policy",
    # Protocol policies (wave 2026-07-15).
    ("fvTenant", "fc_pinning"): "fibre_channel_uplink_pinning_group_profile",
    ("fvCtx", "snmp_context"): "snmp_context_profile",
    ("fvCtx", "dns_label"): "dns_profile_label",
    ("dnsepgSvrGrp", "server"): "dns_server",
    ("dnsepgSvr", "domain"): "dns_domain",
    ("authSvrGroup", "server"): "auth_server",
    ("authSvr", "credential"): "server_credential",
    ("snmpCtxP", "community"): "snmp_community",
    ("qosCustomPol", "dot1p_class"): "dot1p_class_to_priority_mapping_policy",
    ("qosCustomPol", "dscp_class"): "dscp_class_to_priority_mapping_policy",
    ("fvRsFcPathAtt", "pinning_label"): "fibre_channel_uplink_pinning_label",
    ("l3extLoopBackIfP", "node_sid"): "node_sid_profile",
    ("mplsLabelPol", "srgb"): "mpls_srgb_global_configuration",
    ("fvBD", "nd_ra_subnet"): "nd_proxy_subnet",
    ("hsrpGroupP", "secondary_vip"): "secondary_virtual_ip_policy",
    ("l3extRsPathL3OutAtt", "micro_bfd"): "micro_bfd_configuration",
    ("l3extRsPathL3OutAtt", "ptp_l3out"): "ptp_l3out_configuration",
    ("bgpInfraPeerP", "data_plane"): "mdp_data_plane_address",
    (
        "l3extIp",
        "dhcp_relay_gw_ext_ip",
    ): "use_the_external_secondary_address_for_dhcp_relay_gateway",
    ("fvRsPathAtt", "ptp"): "ptp_epg_configuration",
    ("monEPGPol", "target"): "monitoring_target",
    ("vzFilter", "port_zero_entry"): "filter_port_zero_entry",
    ("spanSrcGrp", "vspan_source"): "vspan_vsource",
    ("spanSrcGrp", "vspan_source_def"): "abstract_vspan_source_definition",
    ("spanDest", "vspan_epg_summary"): "vspan_destination_epg_summary",
    # fv remainder (wave 2026-07-15).
    ("fvTenant", "address_pool"): "ip_address_management_pool",
    ("fvAddrMgmtPool", "block"): "ip_address_management_policy",
    ("fvSubnet", "endpoint_network_config"): (
        "client_end_point_network_configuration_policy_for_microsoft"
    ),
    ("fvCtx", "route_summarization"): "vrf_level_route_summarization_policy",
    ("fvCtx", "route_deployment"): "vrf_level_route_deployment_policy",
    ("fvCrtrn", "identity_group"): "identity_group_attribute",
    ("fvCrtrn", "useg_bd"): "container_for_bds",
    ("fvESg", "lif_ctx_selector"): "endpoint_security_group_lifctx_selector",
}


def _makers() -> list[tuple[str, str, str]]:
    makers = _tables().makers
    return [
        (parent, name, child) for parent, table in makers.items() for name, child in table.items()
    ]


def _binds() -> list[tuple[str, str, str]]:
    binds = _tables().binds
    return [
        (owner, alias, target) for owner, table in binds.items() for alias, target in table.items()
    ]


class TestMakers:
    @pytest.mark.parametrize(("parent", "name", "child"), _makers())
    def test_child_class_exists_and_contained(self, parent: str, name: str, child: str) -> None:
        """Every maker's child class is generated and a valid APIC child."""
        assert child in CLASS_PKG, f"{child} not in CLASS_PKG"
        parent_cls = _load_class(parent)
        assert child in parent_cls._contains, f"{child} not a child of {parent}"

    @pytest.mark.parametrize(("parent", "name", "child"), _makers())
    def test_maker_name_matches_facade_jargon(self, parent: str, name: str, child: str) -> None:
        """Curated maker names agree with CHILD_MAP jargon (or are whitelisted)."""
        jargon = {cls: meth for meth, cls in CHILD_MAP.get(parent, {}).items()}
        if child not in jargon:
            return  # class not exposed by facade jargon — nothing to agree with
        expected = _MAKER_RENAMES.get((parent, name), name)
        assert jargon[child] == expected, (
            f"design maker {parent}.{name} → {child} diverges from facade "
            f"jargon {jargon[child]!r} without a documented rename"
        )

    def test_poluni_is_the_single_root_table(self) -> None:
        """Every maker parent is polUni or reachable from it (one rooted tree)."""
        makers = _tables().makers
        reachable = {"polUni"}
        frontier = ["polUni"]
        while frontier:
            for child in makers.get(frontier.pop(), {}).values():
                if child not in reachable:
                    reachable.add(child)
                    frontier.append(child)
        orphans = set(makers) - reachable
        assert not orphans, f"maker tables unreachable from polUni: {sorted(orphans)}"


class TestBinds:
    @pytest.mark.parametrize(("owner", "alias", "target"), _binds())
    def test_edge_resolvable_and_constructible(self, owner: str, alias: str, target: str) -> None:
        """Every bind edge resolves through REFERENCE_MAP with a usable flavor.

        Abstract targets resolve through their concrete subclasses; the Rs
        class must construct (and produce an RN) with the field its flavor
        dictates — ``name`` for tn* relations, ``target_dn`` for tDn ones.
        """
        candidates = [target, *TARGET_SUBCLASSES.get(target, [])]
        direct = {
            entry
            for cand in candidates
            if (entry := REFERENCE_MAP.get(owner, {}).get(cand)) is not None
        }
        inverse = {
            entry
            for cand in candidates
            if (entry := REFERENCE_MAP.get(cand, {}).get(owner)) is not None
        }
        entries = direct or inverse
        assert len(entries) == 1, (
            f"({owner}, {target}) resolves to {sorted(entries)} — need exactly one"
        )
        rs, flavor = next(iter(entries))
        fields = {"name": "x"} if flavor == "name" else {"target_dn": "uni/x"}
        rs_mo = _load_class(rs).model_validate(fields)
        assert rs_mo.rn, f"{rs} produced an empty RN"


class TestVerbs:
    def test_verb_rs_classes_constructible(self) -> None:
        """provide/consume Rs classes exist and take name=."""
        verbs = _tables().verbs
        assert verbs, "verbs table is empty"
        for table in verbs.values():
            for spec in table.values():
                naming: dict[str, Any] = {"name": "x"}
                rs_mo = _load_class(spec["rs"])(**naming)
                assert rs_mo.rn
                assert spec["target"] in CLASS_PKG


class TestSugar:
    def test_sugar_params_are_consumed_by_the_runtime(self) -> None:
        """Every declared sugar parameter is rewritten by design._sugar.

        A sugar key that survives ``apply_sugar`` untouched would reach the
        Pydantic model as an unknown field — the declaration and the runtime
        must stay in lock-step.
        """
        for aci_class, params in _tables().sugar.items():
            assert aci_class in CLASS_PKG
            for param in params:
                rewritten = apply_sugar(aci_class, {param: 80})
                assert param not in rewritten, (
                    f"sugar {aci_class}.{param} is not handled by apply_sugar"
                )


class TestAtomic:
    def test_atomic_classes_are_curated_makers(self) -> None:
        """Atomic classes exist and appear as a maker child (else unreachable)."""
        makers = _tables().makers
        curated_children = {c for table in makers.values() for c in table.values()}
        for aci_class in _tables().atomic:
            assert aci_class in CLASS_PKG
            assert aci_class in curated_children, f"atomic {aci_class} is not a curated child"
