"""Act 3 — tenant: the three-tier application, published on act-2's cabling.

One mental model, end to end (ADR-001): **describe with the design DSL,
apply with push, observe with the facade**.

1. **Plan → strict push → idempotence** — the app in operator vocabulary,
   dry-run first, one atomic POST, replan clean, drift pinpointed.
2. **Staged push** — the same DSL compiled to per-object waves.
3. **Day-2 declaratively** — a description patch and a drift check are just
   smaller designs: set + push, audited by plan (D-1/D-7).
4. **The bridge to the physical world** — EPG domain attach through the
   abstract ``domain`` bind (declared phys-dom) and through ``bind_dn``
   (raw DN, D-3b), then the static VPC path as a literal-DN maker.
5. **Query audit** and an explicit ``delete()`` on a sacrificial tenant —
   the facade observes and deletes; it never configures.

No act cleans up at the END of its run: the walkthrough state stays on the
simulator for manual investigation, and each act wipes what it owns at its
START instead (deterministic re-runs).
"""

from __future__ import annotations

import pytest

from niwaki import Niwaki
from niwaki.design import Cursor, PlanResult, PushReport, tenant
from niwaki.exceptions import NotFoundError
from niwaki.models.fv.fvBD import fvBD
from niwaki.models.tag.tagTag import tagTag
from tests.integration.conftest import (
    PHYS_DOM,
    PHYS_DOM_DN,
    TENANT,
    TENANT_DEV,
    VPC_PATH_DN,
    wipe_tenant,
)

pytestmark = pytest.mark.integration


def showcase_design() -> Cursor:
    """Three-tier app in operator vocabulary — no ACI class in sight.

    Note: filter ports deliberately avoid 80/443 — the APIC normalises those
    to their named forms ("http"/"https") on read, which would defeat the
    plan-idempotence demonstration.
    """
    return (
        tenant(TENANT, description="niwaki walkthrough act 3")
        .app("shop")
            .epg("frontend").bind(bd="frontend").consume("web-api")
            .epg("backend").bind(bd="backend").provide("web-api").consume("db")
            .epg("database").bind(bd="database").provide("db")
        .bd("frontend")
            .set(unicast_routing=True, arp_flooding=True)
            .subnet("10.10.1.1/24")
            .bind(vrf="prod")
        .bd("backend")
            .set(unicast_routing=True)
            .subnet("10.10.2.1/24")
            .bind(vrf="prod")
        .bd("database")
            .set(unicast_routing=False, arp_flooding=True)  # pure L2 tier
            .bind(vrf="prod")
        .vrf("prod")
        .filter("web")
            .entry("api", tcp=8080)
        .filter("db")
            .entry("postgres", tcp=5432)
        .contract("web-api")
            .set(scope="vrf")
            .subject("api").bind(filter="web")
        .contract("db")
            .subject("sql").bind(filter="db")
    )  # fmt: skip


class Test3TenantApplication:
    """Day-N: the application, from dry run to static path binding."""

    # ── 1 · Design DSL: plan → atomic push → idempotence ────────────────────

    def test_01_plan_is_a_pure_dry_run(self, live_aci: Niwaki) -> None:
        wipe_tenant(live_aci)
        plan = showcase_design().push(live_aci, mode="plan")

        assert isinstance(plan, PlanResult)
        assert plan.has_changes
        assert f"uni/tn-{TENANT}" in plan.creates
        assert plan.updates == {}
        with pytest.raises(NotFoundError):
            live_aci.tenant(TENANT).read()

    def test_02_strict_push_is_one_atomic_post(self, live_aci: Niwaki) -> None:
        report = showcase_design().push(live_aci, mode="strict")

        assert isinstance(report, PushReport)
        assert report.request_count == 1
        assert live_aci.tenant(TENANT).bd("frontend").read().unicast_routing is True

    def test_03_replan_is_idempotent(self, live_aci: Niwaki) -> None:
        plan = showcase_design().push(live_aci, mode="plan")
        assert not plan.has_changes
        assert f"uni/tn-{TENANT}" in plan.unchanged

    def test_04_plan_pinpoints_a_drift(self, live_aci: Niwaki) -> None:
        # Simulate out-of-band drift with a minimal counter-design.
        tenant(TENANT).bd("frontend").set(unicast_routing=False).push(live_aci)

        plan = showcase_design().push(live_aci, mode="plan")
        bd_dn = f"uni/tn-{TENANT}/BD-frontend"
        assert plan.updates == {bd_dn: {"unicast_routing": (False, True)}}

        showcase_design().push(live_aci, mode="strict")  # converge back
        assert not showcase_design().push(live_aci, mode="plan").has_changes

    # ── 2 · Staged push: same DSL, one op per object ─────────────────────────

    def test_05_staged_push_runs_in_waves(self, live_aci: Niwaki) -> None:
        dev = (
            tenant(TENANT_DEV, description="niwaki staged-mode demo")
            .bd("sandbox").set(unicast_routing=True).bind(vrf="dev")
            .vrf("dev")
        )  # fmt: skip

        report = dev.push(live_aci, mode="staged")

        assert report.request_count == 4  # tenant, bd, rsctx, vrf
        assert report.dns[0] == f"uni/tn-{TENANT_DEV}"

    # ── 3 · Day-2 declaratively: smaller designs, same verbs ─────────────────

    def test_06_day2_patch_is_a_small_design(self, live_aci: Niwaki) -> None:
        """Only the declared field travels — the rest is untouched (D-1)."""
        patch = tenant(TENANT).bd("backend").set(description="backend tier - patched declaratively")
        patch.push(live_aci)

        mo = live_aci.tenant(TENANT).bd("backend").read()
        assert mo.description == "backend tier - patched declaratively"
        assert mo.unicast_routing is True  # untouched

    def test_07_plan_writes_only_on_drift(self, live_aci: Niwaki) -> None:
        """Write only on drift, declaratively: the plan says when to push."""
        desired = tenant(TENANT).bd("database").set(arp_flooding=True)
        assert not desired.push(live_aci, mode="plan").has_changes  # already there

        flipped = tenant(TENANT).bd("database").set(arp_flooding=False)
        plan = flipped.push(live_aci, mode="plan")
        assert plan.updates == {f"uni/tn-{TENANT}/BD-database": {"arp_flooding": (True, False)}}
        flipped.push(live_aci)
        assert not flipped.push(live_aci, mode="plan").has_changes

    def test_08_new_subtree_is_a_design_too(self, live_aci: Niwaki) -> None:
        """A BD + subnet + VRF reference — closed world, so the existing VRF
        is re-declared as an attribute-less upsert (D-1)."""
        cfg = tenant(TENANT)
        cfg.vrf("prod")  # upsert — already exists, nothing changes
        cfg.bd("typed-path", unicast_routing=True).subnet("10.10.9.1/24", scope="private").bind(
            vrf="prod"
        )
        cfg.push(live_aci)

        assert live_aci.tenant(TENANT).bd("typed-path").query("fvSubnet").count() == 1

    # ── 4 · The bridge to acts 1-2: domain attach + static VPC path ──────────

    def test_09_epgs_attach_to_the_domain(self, live_aci: Niwaki) -> None:
        """``fvRsDomAtt`` links each EPG to act-2's physical domain.

        Two equivalent spellings: declare the phys-dom in the design and
        ``bind(domain=...)`` (closed world, abstract target), or reference it
        by raw DN with ``bind_dn`` (D-3b) — no lookup, the APIC arbitrates.
        """
        cfg = tenant(TENANT)
        cfg.phys_dom(PHYS_DOM)  # cross-domain upsert of act-2's domain
        app = cfg.app("shop")
        app.epg("frontend").bind(domain=PHYS_DOM)
        app.epg("backend").bind(domain=PHYS_DOM)
        app.epg("database").bind_dn(domain=PHYS_DOM_DN)
        cfg.push(live_aci)

        assert live_aci.tenant(TENANT).query("fvRsDomAtt").count() == 3

    def test_10_static_vpc_path_binding(self, live_aci: Niwaki) -> None:
        """``static_path`` maps the frontend EPG on the act-2 VPC, tagged
        vlan-201 — the everyday "plug a workload" operation, as a literal-DN
        maker (the path DN lives outside ``uni``)."""
        cfg = tenant(TENANT)
        cfg.app("shop").epg("frontend").static_path(VPC_PATH_DN, encap="vlan-201", mode="regular")
        cfg.push(live_aci)

        paths = live_aci.tenant(TENANT).query("fvRsPathAtt").fetch()
        assert [p.encap for p in paths] == ["vlan-201"]

    # ── 5 · Escape hatch + audit ──────────────────────────────────────────────

    def test_11_mo_escape_hatch(self, live_aci: Niwaki) -> None:
        cfg = tenant(TENANT)
        cfg.mo(tagTag, key="managed-by", value="niwaki-sdk")
        cfg.push(live_aci)

        tags = live_aci.tenant(TENANT).query("tagTag").fetch()
        assert any(t.key == "managed-by" for t in tags)

    def test_12_query_inventory(self, live_aci: Niwaki) -> None:
        tn = live_aci.tenant(TENANT)
        assert tn.query(fvBD).count() == 4  # frontend, backend, database, typed-path
        assert tn.query("fvSubnet").count() == 3  # the L2 database BD has none
        providers = {rs.name for rs in tn.query("fvRsProv").fetch()}
        assert providers == {"web-api", "db"}

    # ── 99 · Delete is an explicit, observable act (facade lifecycle) ────────
    #
    # No end-of-run cleanup: the walkthrough state stays on the simulator for
    # manual investigation.  Each act wipes what it owns at its START, so
    # re-runs stay deterministic.  This test still proves delete() works — on
    # a sacrificial tenant created for that purpose.

    def test_99_delete_is_explicit(self, live_aci: Niwaki) -> None:
        victim = f"{TENANT}-delete-me"
        tenant(victim).push(live_aci)
        live_aci.tenant(victim).read()  # exists

        live_aci.tenant(victim).delete()
        with pytest.raises(NotFoundError):
            live_aci.tenant(victim).read()


def protocol_policies_design() -> Cursor:
    """Every Tenant > Policies > Protocol folder, property combinations covered.

    One design, pure DSL: each GUI folder contributes as many instances as it
    takes to exercise every enum value and the salient booleans/integers at
    least once.  Secure fields (authentication keys) are deliberately left
    out — the APIC never echoes them, which would fake a drift at replan.
    The replan-converged assertion is the property round-trip proof.
    """
    cfg = tenant(TENANT)

    # ── OSPF ──────────────────────────────────────────────────────────────────
    cfg.ospf_interface_policy(
        "ospf-p2p",
        network_type="p2p",
        cost_of_interface="100",
        hello_interval="5",
        dead_interval="20",
        prefix_suppression="enable",
    )
    cfg.ospf_interface_policy(
        "ospf-bcast",
        network_type="bcast",
        prioriity="42",
        retransmit_interval="10",
        transmit_delay="3",
        prefix_suppression="disable",
    )
    cfg.ospf_timers_policy(
        "ospf-timers-log",
        action="log",
        maximum_of_non_self_generated_lsas=10000,
        bandwidth_preference=80000,
        max_ecmp="6",
    )
    cfg.ospf_timers_policy("ospf-timers-reject", action="reject", reset_interval=8)

    # ── EIGRP ─────────────────────────────────────────────────────────────────
    cfg.eigrp_interface_policy(
        "eigrp-pico",
        units_for_eigrp_interface_delay="pico",
        eigrp_interface_delay=100,
        eigrp_interface_bandwidth=100000,
        interface_controls="split-horizon",
    )
    cfg.eigrp_interface_policy(
        "eigrp-micro",
        units_for_eigrp_interface_delay="tens-of-micro",
        hello_interval="10",
        hold_interval="30",
    )
    cfg.eigrp_address_family_context_policy(
        "eigrp-af-narrow",
        metric_style="narrow",
        maximum_ecmp_paths="4",
    )
    cfg.eigrp_address_family_context_policy(
        "eigrp-af-wide",
        metric_style="wide",
        internal_distance="90",
        external_distance="170",
    )

    # ── BGP ───────────────────────────────────────────────────────────────────
    cfg.bgp_timers_policy(
        "bgp-timers",
        hold_interval="90",
        keepalive_interval="30",
        # 300 is the default and reads back as the literal "default" —
        # a non-default value keeps the round-trip honest.
        max_as_limit="50",
        stale_interval="600",
    )
    cfg.bgp_address_family_context_policy(
        "bgp-af",
        ebgp_distance="20",
        ibgp_distance="200",
        local_distance="220",
        max_ecmp_for_ebgp_routes="8",
        max_ecmp_for_ibgp_routes="8",
        max_local_ecmp_for_redistribute_rotes="8",
    )
    for action in ("log", "reject", "restart", "shut"):
        peer_prefix = cfg.bgp_peer_prefix_policy(
            f"bgp-pfx-{action}",
            max_prefix_action=action,
            max_number_of_prefixes=20000,
            warning_threshold="75",
        )
        if action == "restart":
            peer_prefix.set(prefix_limit_restart_time="5")
    cfg.bgp_best_path_control_policy("bgp-bp-relax", best_path_control="asPathMultipathRelax")
    cfg.bgp_best_path_control_policy("bgp-bp-default")

    # ── Route summarization (BGP / EIGRP / OSPF) ─────────────────────────────
    cfg.bgp_route_summarization_policy("bgp-summ")
    cfg.eigrp_route_summarization_policy("eigrp-summ")
    cfg.ospf_route_summarization_policy(
        "ospf-summ",
        inter_area_summarization_enabled=True,
        area_range_cost=120,
        summary_route_tag=777,
    )

    # ── HSRP ──────────────────────────────────────────────────────────────────
    cfg.hsrp_interface_policy(
        "hsrp-if",
        hsrp_interface_delay="30",
        hsrp_reload_delay="60",
    )
    cfg.hsrp_group_policy(
        "hsrp-grp-preempt",
        group_control_bits="preempt",
        group_priority="120",
        hello_interval=1000,
        hold_interval=3000,
        miminum_delay_before_preempt="30",
    )
    cfg.hsrp_group_policy("hsrp-grp-plain", group_priority="90")

    # ── IGMP / PIM interface ──────────────────────────────────────────────────
    cfg.igmp_interface_policy(
        "igmp-v2",
        version="v2",
        query_interval="125",
        response_interval="10",
    )
    cfg.igmp_interface_policy(
        "igmp-v3",
        version="v3",
        last_member_query_count="3",
        robustness_factor="3",
        startup_query_count="3",
    )
    cfg.pim_interface_policy(
        "pim-dr",
        authentication_type="none",
        designated_router_priority=10,
        hello_interval=30000,
        join_prune_interval_seconds="60",
    )

    # ── BFD ───────────────────────────────────────────────────────────────────
    cfg.bfd_interface_policy(
        "bfd-echo",
        enable_disable_sessions="enabled",
        enable_disable_echo_mode="enabled",
        echo_rx_interval="50",
        detection_multiplier="3",
    )
    cfg.bfd_interface_policy(
        "bfd-noecho",
        enable_disable_sessions="disabled",
        enable_disable_echo_mode="disabled",
        required_minimum_rx_interval="250",
        desired_minimum_tx_interval="250",
    )
    cfg.bfd_mh_interface_policy(
        "bfdmh-on",
        enable_disable_sessions="enabled",
        detection_multiplier="5",
    )
    cfg.bfd_mh_interface_policy("bfdmh-off", enable_disable_sessions="disabled")
    cfg.bfd_multihop_node_policy(
        "bfdmh-node",
        detection_multiplier="4",
        required_minimum_rx_interval="300",
        desired_minimum_tx_interval="300",
    )

    # ── ND / ARP ──────────────────────────────────────────────────────────────
    cfg.nd_interface_policy(
        "nd-fast",
        hop_limit="64",
        mtu="1500",
        neighbor_solicit_interval=1000,
        router_advertisement_interval="600",
    )
    cfg.nd_interface_policy(
        "nd-jumbo",
        hop_limit="255",
        mtu="9000",
        reachable_time=30000,
    )
    cfg.nd_ra_prefix_policy(
        "nd-pfx",
        valid_lifetime=2592000,
        preferred_lifetime=604800,
    )
    cfg.arp_interface_policy("arp-garp", interface_controls_for_arp="garp-adj-enable")
    # "unspecified" is stored as "" by the APIC — leave the field unset on
    # the plain instance so the plan round-trip stays clean.
    cfg.arp_interface_policy("arp-plain")

    # ── QoS: custom + data-plane policing ─────────────────────────────────────
    cfg.custom_qos_policy("qos-custom")
    cfg.dpp_policy(
        "dpp-mark",
        admin_st="enabled",
        bit_or_packet="bit",
        burst="1000",
        burst_unit="kilo",
        peak_rate="500",
        peak_rate_unit="mega",
        confirm_action="mark",
        conform_mark_cos="2",
        conform_mark_dscp="26",
        exceed_action="drop",
    )
    cfg.dpp_policy(
        "dpp-tx",
        admin_st="disabled",
        bit_or_packet="packet",
        burst="5000",
        burst_unit="mega",
        confirm_action="transmit",
        exceed_action="transmit",
    )

    # ── Endpoint retention / snooping ─────────────────────────────────────────
    cfg.ep_retention_policy(
        "ep-fast",
        local_ep_age_interval="300",
        remote_ep_age_interval="200",
        ep_bounce_age_interval="500",
        ep_move_frequency="128",
    )
    cfg.igmp_snoop_policy(
        "igsn-on",
        admin_state="enabled",
        version="v3",
        query_interval="100",
    )
    cfg.igmp_snoop_policy("igsn-off", admin_state="disabled", version="v2")
    cfg.mld_snoop_policy(
        "mldsn-on",
        admin_state="enabled",
        version="v2",
        response_interval="8",
    )
    cfg.mld_snoop_policy("mldsn-off", admin_state="disabled", version="v1")

    # ── First Hop Security ────────────────────────────────────────────────────
    cfg.fhs_bd_policy(
        "fhs-full",
        ip_inspection_admin_status="enabled-both",
        router_advertisement_guard_admin_status="enabled",
        source_guard_admin_status="enabled-both",
    )
    cfg.fhs_bd_policy(
        "fhs-v4",
        ip_inspection_admin_status="enabled-ipv4",
        router_advertisement_guard_admin_status="disabled",
        source_guard_admin_status="enabled-ipv4",
    )
    cfg.trust_control_policy(
        "trust-all",
        contains_dhcpv4_servers=True,
        contains_dhcpv6_servers=True,
        contains_ipv6_routers=True,
        trust_arp=True,
        trust_nd=True,
        trust_router_advertisement=True,
    )
    cfg.trust_control_policy(
        "trust-arp-only",
        trust_arp=True,
        trust_nd=False,
    )

    # ── IP SLA + track lists / members ────────────────────────────────────────
    cfg.ip_sla_monitoring_policy(
        "sla-icmp",
        sla_type="icmp",
        frequency="30",
        detect_multiplier="3",
    ).icmp_echo_probe()
    cfg.ip_sla_monitoring_policy(
        "sla-tcp",
        sla_type="tcp",
        port="8443",
        operation_timeout=900,
    ).tcp_probe()
    cfg.ip_sla_monitoring_policy(
        "sla-http",
        sla_type="http",
        port="80",
        http_method_used_for_probing="get",
        uri_for_http_probing="/health",
        http_version_used_for_probing="HTTP11",
    )
    cfg.track_list(
        "trk-pct",
        type_of_tracklist="percentage",
        percentage_up="60",
        percentage_down="40",
    )
    cfg.track_list(
        "trk-weight",
        type_of_tracklist="weight",
        weight_up_value="10",
        weight_down_value="5",
    )
    cfg.track_member(
        "trk-gw",
        destination_ip_to_be_tracked="192.0.2.1",
        scope_of_track_member=f"uni/tn-{TENANT}/BD-frontend",
    ).bind(ip_sla_monitoring_policy="sla-icmp")

    # ── Multicast route maps / route tag / ext bridge group ─────────────────
    mcast = cfg.pim_route_map_policy("mcast-rmap")
    mcast.pim_route_map_entry(
        "10",
        action="permit",
        source_filter="10.99.0.0/24",
        destination_filter="225.1.0.0/16",
    )
    mcast.pim_route_map_entry("20", action="deny", rp_ip_address="10.99.9.9")
    cfg.route_tag_policy("rtag-a", route_tag=4001)
    cfg.route_tag_policy("rtag-b", route_tag=4002)
    cfg.external_bridge_group_profile("ext-bd-grp")

    # ── Route control (tenant-level), match & set rules, keychain, MPLS ──────
    cfg.route_control_profile("tenant-rmap").route_control_context(
        "permit-all", action="permit", local_order="0"
    )
    cfg.match_rule("match-basic")
    cfg.action_rule_profile("set-basic")
    # Keychain with its EIGRP key table fully configured — the pre-shared
    # key is write-only (never echoed), so the differ skips it by design.
    keychain = cfg.tenant_keychain_policy("keychain")
    keychain.key_policy(
        "1",
        name="rollover-a",
        pre_shared_key="niwaki-key-a",
        start_time="2026-07-01T00:00:00.000+00:00",
        end_time="2027-01-01T00:00:00.000+00:00",
    )
    keychain.key_policy(
        "2",
        name="rollover-b",
        pre_shared_key="niwaki-key-b",
        start_time="2027-01-01T00:00:00.000+00:00",
    )
    cfg.mpls_interface_policy("mpls-if")
    # mpls_global_configuration (mplsLabelPol) is APIC-restricted to the
    # infra tenant — exercised there, not in a user tenant.

    # ── DHCP ──────────────────────────────────────────────────────────────────
    relay = cfg.dhcp_relay_policy("dhcp-visible", relay_mode="visible", owner="tenant")
    relay.provider(f"uni/tn-{TENANT}/ap-shop/epg-backend", dhcp_server_address="10.10.2.53")
    # relay_mode="not-visible" is rejected by this APIC release ("mode
    # not-visible is not supported") — visible is the only accepted value.
    cfg.dhcp_relay_policy("dhcp-plain")
    options = cfg.dhcp_option_policy("dhcp-opts")
    options.dhcp_option("dns-server", id="6", model_regex="10.10.2.53")
    options.dhcp_option("domain-name", id="15", model_regex="shop.example")

    # ── L4-L7 standalone (PBR chain) ─────────────────────────────────────────
    svc = cfg.service_container()
    pbr = svc.service_redirect_policy(
        "pbr-l3",
        dest_type="L3",
        hashing_algorithm="sip-dip-prototype",
        resilient_hashing_enabled_or_not=True,
        threshold_enable=True,
        minimum_threshold_percentage="20",
        maximum_threshold_percentage="80",
        threshold_down_action="deny",
    )
    pbr.destination_of_redirected_traffic("10.20.99.1", mac_address="00:00:0A:14:63:01")
    pbr.destination_of_redirected_traffic(
        "10.20.99.2", mac_address="00:00:0A:14:63:02", weight=5
    ).bind(l4_l7_redirect_health_group="pbr-health")
    svc.l4_l7_redirect_health_group("pbr-health")
    svc.pbr_backup_policy("pbr-backup").destination_of_redirected_traffic(
        "10.20.99.9", mac_address="00:00:0A:14:63:09"
    )
    svc.service_epg_policy("svc-epg-incl", preferred_group_member="include")
    svc.service_epg_policy("svc-epg-excl", preferred_group_member="exclude")

    return cfg


class Test3ProtocolPolicies:
    """Tenant > Policies > Protocol — every folder, property combos, live.

    Coverage argument: each enum value and salient boolean/integer appears in
    at least one instance, and test_02 proves the whole set round-trips —
    plan compares every declared attribute against what the fabric stores,
    so a single non-persisted property breaks convergence.
    """

    def test_01_the_full_protocol_set_pushes_atomically(self, live_aci: Niwaki) -> None:
        design = protocol_policies_design()
        plan = design.push(live_aci, mode="plan")
        assert plan.has_changes

        report = design.push(live_aci)
        assert report.request_count == 1

    def test_02_every_property_round_trips(self, live_aci: Niwaki) -> None:
        assert protocol_policies_design().push(live_aci, mode="plan").has_changes is False

    def test_03_typed_readback_spot_checks(self, live_aci: Niwaki) -> None:
        tn = live_aci.tenant(TENANT)

        ospf = {p.name: p for p in tn.query("ospfIfPol").fetch()}
        assert ospf["ospf-p2p"].network_type == "p2p"
        assert ospf["ospf-bcast"].network_type == "bcast"

        prefix_policies = {p.name: p for p in tn.query("bgpPeerPfxPol").fetch()}
        assert {p.max_prefix_action for p in prefix_policies.values()} == {
            "log",
            "reject",
            "restart",
            "shut",
        }

        trust = {p.name: p for p in tn.query("fhsTrustCtrlPol").fetch()}
        assert trust["trust-all"].trust_nd is True
        assert trust["trust-arp-only"].trust_nd is False

        slas = {p.name: p for p in tn.query("fvIPSLAMonitoringPol").fetch()}
        assert {p.sla_type for p in slas.values()} >= {"icmp", "tcp", "http"}

        entries = tn.query("pimRouteMapEntry").fetch()
        assert {e.action for e in entries} == {"permit", "deny"}

        destinations = tn.query("vnsRedirectDest").fetch()
        assert len(destinations) == 3

        keys = {k.key_id: k for k in tn.query("fvKeyPol").fetch()}
        assert set(keys) == {"1", "2"}
        assert keys["1"].name == "rollover-a"
        assert keys["1"].pre_shared_key == ""  # write-only: never echoed

        options = {o.name: o for o in tn.query("dhcpOption").fetch()}
        assert options["dns-server"].model_regex == "10.10.2.53"
        assert options["domain-name"].id == "15"
