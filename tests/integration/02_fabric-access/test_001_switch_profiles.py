"""Fabric access — switch profiles and leaf switch policy groups.

Run:
    uv run pytest tests/integration/02_fabric-access/test_001_switch_profiles.py -m integration -s

Provisioning the fabric starts with:

- a **switch profile per switch**, named after the node (``leaf-101``,
  ``spine-1001``) — a leaf/spine profile + selector, each selecting its single
  node by ID;
- a **leaf switch policy group per leaf** (``infraAccNodePGrp``), named after the
  leaf, ready to carry that switch's node-level policies;
- the **switch policies** themselves — BFD (single-hop + multihop, v4/v6), CoPP
  and CoPP pre-filter (leaf + spine), forwarding scale, fast link failover,
  Fibre Channel, NetFlow, PoE, PTP, SyncE, USB, 802.1x and flash — each a named,
  production-configured object with an explanatory description.  Spanning Tree
  (MST) is a fabric singleton, so it is configured on ``default`` (BPDU filter
  for extended-chassis ports + the region policy).
"""

from __future__ import annotations

import pytest

from niwaki import Niwaki
from niwaki.design import infra

pytestmark = pytest.mark.integration


def test_switch_profiles(live_aci: Niwaki) -> None:
    fab = infra()
    for node in live_aci.query("fabricNode").fetch():
        data = node.model_dump(by_alias=True)
        role, name, node_id = data.get("role"), data.get("name"), data.get("id")
        if role not in ("leaf", "spine") or not name or not node_id:
            continue
        nid = int(node_id)

        # One switch profile per node, named after it, selecting only that node.
        if role == "leaf":
            selector = fab.leaf_profile(name).leaf_selector(name, selector_type="range")
        else:
            selector = fab.spine_profile(name).spine_selector(name, selector_type="range")
        selector.node_block(name, from_node_id=nid, to_node_id=nid)

    fab.push(live_aci)


def test_leaf_switch_policy_groups(live_aci: Niwaki) -> None:
    fab = infra()
    func = fab.func_profile()
    for node in live_aci.query("fabricNode").fetch():
        data = node.model_dump(by_alias=True)
        if data.get("role") == "leaf" and data.get("name"):
            func.leaf_switch_group(data["name"])  # one leaf switch policy group per leaf

    fab.push(live_aci)


def test_switch_policies(live_aci: Niwaki) -> None:
    fab = infra()

    # ── BFD, single-hop and multihop, IPv4 + IPv6 ───────────────────────────
    fab.bfd_global_ipv4_policy(
        "bfd-ipv4-250x3",
        description="IPv4 BFD: 250 ms tx/rx with a 3x detection multiplier.",
        detection_multiplier=3,
        desired_minimum_tx_interval=250,
        required_minimum_rx_interval=250,
    )
    fab.bfd_global_ipv6_policy(
        "bfd-ipv6-250x3",
        description="IPv6 BFD: 250 ms tx/rx with a 3x detection multiplier.",
        detection_multiplier=3,
        desired_minimum_tx_interval=250,
        required_minimum_rx_interval=250,
    )
    fab.bfd_global_ipv4_mh_policy(
        "bfd-mh-ipv4-250x3",
        description="IPv4 multihop BFD: 250 ms tx/rx, 3x detection multiplier.",
        detection_multiplier=3,
        desired_minimum_tx_interval=250,
        required_minimum_rx_interval=250,
    )
    fab.bfd_global_ipv6_mh_policy(
        "bfd-mh-ipv6-250x3",
        description="IPv6 multihop BFD: 250 ms tx/rx, 3x detection multiplier.",
        detection_multiplier=3,
        desired_minimum_tx_interval=250,
        required_minimum_rx_interval=250,
    )

    # ── Control-plane policing (leaf) ───────────────────────────────────────
    fab.copp_leaf_policy(
        "copp-leaf-strict",
        description="Leaf control-plane policing at the strict preset.",
        type_of_profile="strict",
    )
    fab.copp_prefilter_leaf_policy(
        "copp-prefilter-leaf",
        description="Leaf CoPP pre-filter - permit list evaluated ahead of CoPP.",
    )

    # ── Forwarding scale, fast link failover ────────────────────────────────
    fab.forwarding_scale_profile_policy(
        "fwdscale-dual-stack",
        description="Dual-stack forwarding scale profile (balanced v4/v6).",
        fwd_scale_profile_type="dual-stack",
    )
    fab.fast_link_failover_policy(
        "flf-enabled",
        description="Fast link failover enabled for sub-second uplink recovery.",
        fast_link_failover_mode_type="on",
    )

    # ── Fibre Channel ───────────────────────────────────────────────────────
    fab.fc_instance_policy(
        "fc-node",
        description="Fibre Channel node policy with an 8 s FIP keepalive.",
        fip_keepalive_interval=8,
    )
    fab.fc_fabric_policy(
        "fc-san",
        description="FC SAN timers: 2 s error-detect, 10 s resource-allocation.",
        fc_protocol_error_detect_timeout=2000,
        fc_protocol_resource_allocation_timeout=10000,
    )

    # ── NetFlow, PoE, PTP ───────────────────────────────────────────────────
    fab.netflow_node_policy(
        "netflow-node",
        description="NetFlow node: 300 s collection, 600 s template, 1500 B MTU.",
        collection_interval_in_seconds=300,
        template_interval_in_seconds=600,
        mtu=1500,
    )
    fab.poe_policy(
        "poe-combined",
        description="PoE combined power control, 30 W default per-port budget.",
        power_control="combined",
        consumption_default=30000,
    )
    fab.ptp_node_policy(
        "ptp-hybrid",
        description="PTP node, hybrid mode, domain 24 (Telecom-8275-1), priority 128.",
        ptp_operating_mode="hybrid",
        ptp_node_level_domain=24,
        ptp_node_level_priority1=128,
        ptp_node_level_priority2=128,
    )

    # ── Spanning Tree (MST) — a fabric singleton, configured on "default" ───
    mst = fab.mst_policy(
        "default",
        description="Fabric MST: BPDU filter enabled on extended-chassis ports.",
        controls="extchp-bpdu-filter",
    )
    mst.mst_region(
        "default",
        description="Fabric MST region (acme, revision 1).",
        region_name="acme",
        region_revision=1,
    )

    # ── SyncE, USB, 802.1x, flash ───────────────────────────────────────────
    fab.synce_policy(
        "synce-op1",
        description="Synchronous Ethernet enabled, QL option 1.",
        admin_state="enabled",
        ql_option_type_node="op1",
    )
    fab.usb_configuration_policy(
        "usb-disabled",
        description="USB ports disabled on the switches (hardening).",
        disable_usb_ports=True,
    )
    fab.dot1x_node_authentication(
        "dot1x-node",
        description="802.1x node authentication, fail-auth to the quarantine EPG.",
        fail_auth_vlan="vlan-333",
        end_point_group="tn-no-mans-land,ap-access,epg-quarantine",
    ).bind_dn(radius_provider_group="uni/userext/radiusext/radiusprovidergroup-dot1x-radius")
    fab.flash_configuration_policy(
        "flash-monitoring",
        description="Flash wear-out monitoring thresholds.",
    )

    # ── Control-plane policing (spine) ──────────────────────────────────────
    fab.copp_spine_policy(
        "copp-spine-strict",
        description="Spine control-plane policing at the strict preset.",
        type_of_profile="strict",
    )
    fab.copp_prefilter_spine_policy(
        "copp-prefilter-spine",
        description="Spine CoPP pre-filter - permit list evaluated ahead of CoPP.",
    )

    fab.push(live_aci)
