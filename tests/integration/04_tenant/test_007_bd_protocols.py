"""Tenant — bridge-domain protocols, combination-exhaustive (non-prod).

Run:
    uv run pytest tests/integration/04_tenant/test_007_bd_protocols.py -m integration -s

The protocol surface hanging off a bridge domain: IGMP/MLD snooping, neighbour
discovery, DHCP relay, first-hop security, and BD-level PIM. Each referenced
policy is created in several variants covering its enums (IGMP/MLD snoop admin
state, version and control flags; ND interface control flags; DHCP relay mode;
FHS inspection/guard admin states), and the BD children — DHCP relay labels with
an option policy, ND router-advertisement subnets, the BD PIM policy with its
multicast filter, and rogue-exception MACs — are attached.

Values are illustrative. This file owns tenant ``niwaki-it-bd-proto``; ``wipe``
(operator-only) deletes it.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import tenant
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

TN = "niwaki-it-bd-proto"
VRF = "niwaki-it-bd-proto-vrf"


def test_bd_snooping_and_nd(live_aci: Niwaki) -> None:
    """IGMP/MLD snoop and ND policies (enum variants) bound onto BDs, plus children."""
    tn = tenant(TN, description="BD protocols - IGMP/MLD snoop, ND, DHCP relay, FHS, PIM")
    tn.vrf(VRF, description="VRF backing the protocol BDs.")

    tn.igmp_snoop_policy(
        "niwaki-it-igmp-v3",
        description="IGMP snoop, enabled, v3, querier + fast-leave.",
        admin_state="enabled",
        version="v3",
        controls="querier,fast-leave",
        query_interval=125,
    )
    tn.igmp_snoop_policy(
        "niwaki-it-igmp-v2",
        description="IGMP snoop, disabled, v2, opt-flood + routing.",
        admin_state="disabled",
        version="v2",
        controls="opt-flood,routing",
    )
    tn.mld_snoop_policy(
        "niwaki-it-mld-v2",
        description="MLD snoop, enabled, v2, fast-leave.",
        admin_state="enabled",
        version="v2",
        controls="fast-leave",
    )
    tn.mld_snoop_policy(
        "niwaki-it-mld-v1",
        description="MLD snoop, disabled, v1, no controls.",
        admin_state="disabled",
        version="v1",
        controls="",
    )
    tn.nd_interface_policy(
        "niwaki-it-nd-managed",
        description="ND interface policy, managed + other config.",
        controls="managed-cfg,other-cfg",
        hop_limit=64,
        mtu=1500,
    )
    tn.nd_interface_policy(
        "niwaki-it-nd-suppress",
        description="ND interface policy, suppress RA + glean.",
        controls="suppress-ra,unsolicit-na-glean",
    )

    bd1 = tn.bd(
        "niwaki-it-bd-snoop-a",
        description="BD binding v3 IGMP / v2 MLD / managed ND, multicast enabled.",
        unicast_routing=True,
        multicast_allow=True,
    ).bind(
        vrf=VRF,
        igmp_snoop="niwaki-it-igmp-v3",
        mld_snoop="niwaki-it-mld-v2",
        nd_policy="niwaki-it-nd-managed",
    )
    bd1.nd_ra_subnet("2001:db8:50:1::1/64", description="ND router-advertisement prefix.")
    bd1.pim(description="BD-level PIM policy.").filter(description="BD PIM multicast filter.")

    tn.bd(
        "niwaki-it-bd-snoop-b",
        description="BD binding v2 IGMP / v1 MLD / suppress ND, rogue-except enabled.",
        unicast_routing=True,
        enable_rogue_except_mac=True,
    ).bind(
        vrf=VRF,
        igmp_snoop="niwaki-it-igmp-v2",
        mld_snoop="niwaki-it-mld-v1",
        nd_policy="niwaki-it-nd-suppress",
    ).rogue_exception_mac(
        "00:22:BD:F5:EE:01", description="MAC exempt from rogue-endpoint detection."
    )

    tn.push(live_aci)


def test_bd_dhcp(live_aci: Niwaki) -> None:
    """DHCP relay policies and an option policy, with BD relay labels."""
    tn = tenant(TN, description="BD protocols - IGMP/MLD snoop, ND, DHCP relay, FHS, PIM")
    tn.vrf(VRF, description="VRF backing the DHCP BD.")

    # Relay mode "not-visible" is unsupported on this platform; visible / default.
    tn.dhcp_relay_policy(
        "niwaki-it-dhcp-visible",
        description="DHCP relay policy, visible relay mode.",
        owner="tenant",
        relay_mode="visible",
    )
    tn.dhcp_relay_policy(
        "niwaki-it-dhcp-plain",
        description="DHCP relay policy, default relay mode.",
        owner="tenant",
    )
    tn.dhcp_option_policy(
        "niwaki-it-dhcp-opt", description="DHCP option policy bound onto a relay label."
    )

    bd = tn.bd(
        "niwaki-it-bd-dhcp",
        description="BD carrying DHCP relay labels.",
        unicast_routing=True,
    ).bind(vrf=VRF, dhcp_relay="niwaki-it-dhcp-visible")
    bd.dhcp_relay_label(
        "niwaki-it-dhcp-visible",
        description="Tenant-scoped relay label with option policy.",
        scope="tenant",
        tag="green",
    ).bind(dhcp_option_policy="niwaki-it-dhcp-opt")
    bd.dhcp_relay_label(
        "niwaki-it-dhcp-plain",
        description="Tenant-scoped relay label, default mode.",
        scope="tenant",
        tag="blue",
    )

    tn.push(live_aci)


def test_bd_fhs(live_aci: Niwaki) -> None:
    """First-hop-security BD policies across every inspection/guard admin state."""
    tn = tenant(TN, description="BD protocols - IGMP/MLD snoop, ND, DHCP relay, FHS, PIM")
    tn.vrf(VRF, description="VRF backing the FHS BDs.")

    tn.fhs_bd_policy(
        "niwaki-it-fhs-both",
        description="FHS: inspection both, source-guard both, RA-guard enabled.",
        ip_inspection_admin_status="enabled-both",
        source_guard_admin_status="enabled-both",
        router_advertisement_guard_admin_status="enabled",
    )
    tn.fhs_bd_policy(
        "niwaki-it-fhs-v4",
        description="FHS: inspection IPv4, source-guard IPv4, RA-guard disabled.",
        ip_inspection_admin_status="enabled-ipv4",
        source_guard_admin_status="enabled-ipv4",
        router_advertisement_guard_admin_status="disabled",
    )
    tn.fhs_bd_policy(
        "niwaki-it-fhs-v6",
        description="FHS: inspection IPv6, source-guard disabled.",
        ip_inspection_admin_status="enabled-ipv6",
        source_guard_admin_status="disabled",
    )

    tn.bd(
        "niwaki-it-bd-fhs",
        description="BD binding the both-stack FHS policy.",
        unicast_routing=True,
    ).bind(vrf=VRF, fhs="niwaki-it-fhs-both")

    tn.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (f"uni/tn-{TN}",):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
