"""Tenant — EPG children, combination-exhaustive (non-prod).

Run:
    uv run pytest tests/integration/04_tenant/test_010_epg_children.py -m integration -s

The children hanging off an application EPG: shared subnets; static endpoints
across every type (silent-host / tep / vep) with static IPs; virtual IPs; and
subnet endpoints — NLB (unicast and IGMP modes), anycast, and Microsoft network
configuration — which the APIC accepts under an EPG subnet.

Values are illustrative. This file owns tenant ``niwaki-it-epg-ch``; ``wipe``
(operator-only) deletes it.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import tenant
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

TN = "niwaki-it-epg-ch"
VRF = "niwaki-it-epg-ch-vrf"
BD = "niwaki-it-epg-ch-bd"
APP = "niwaki-it-epg-ch-app"


def _foundation(tn):  # type: ignore[no-untyped-def]
    tn.vrf(VRF, description="VRF backing the EPG-children EPGs.")
    tn.bd(BD, description="BD backing the EPG-children EPGs.", unicast_routing=True).bind(vrf=VRF)


def test_epg_endpoints_and_subnets(live_aci: Niwaki) -> None:
    """An EPG with subnets, static endpoints (every type), static IPs and virtual IPs."""
    tn = tenant(
        TN, description="EPG children - subnets, static endpoints, virtual IPs, subnet endpoints"
    )
    _foundation(tn)
    epg = (
        tn.app(APP, description="Application profile for EPG children.")
        .epg("niwaki-it-epg-eps", description="EPG with static endpoints and virtual IPs.")
        .bind(bd=BD)
    )

    # Shared subnets for route leaking.
    epg.subnet("10.60.1.1/24", description="Private shared subnet.", scope="private,shared")
    epg.subnet("10.60.2.1/24", description="Second private shared subnet.", scope="private,shared")

    # Static endpoints across every type. Only silent-host may carry extra
    # static IPs; a TEP/VEP endpoint cannot have multiple IP addresses.
    epg.static_endpoint(
        "00:60:AA:00:00:01", "silent-host", encap="vlan-2501", ip_address="10.60.1.11"
    ).static_ip("10.60.1.21")
    epg.static_endpoint("00:60:AA:00:00:02", "tep", encap="vlan-2501", ip_address="10.60.1.12")
    epg.static_endpoint("00:60:AA:00:00:03", "vep", encap="vlan-2501", ip_address="10.60.1.13")

    # Virtual IPs.
    epg.virtual_ip("10.60.1.100", description="Service virtual IP.")
    epg.virtual_ip("10.60.1.101", description="Second service virtual IP.")

    tn.push(live_aci)


def test_epg_subnet_endpoints(live_aci: Niwaki) -> None:
    """EPG subnets carrying NLB (both modes), anycast and network-config endpoints."""
    tn = tenant(
        TN, description="EPG children - subnets, static endpoints, virtual IPs, subnet endpoints"
    )
    _foundation(tn)
    epg = (
        tn.app(APP)
        .epg("niwaki-it-epg-subeps", description="EPG exercising subnet endpoint children.")
        .bind(bd=BD)
    )

    # NLB / anycast attach to /32 host subnets that cannot be gateways.
    epg.subnet(
        "10.61.1.1/32",
        description="Host subnet with a unicast NLB endpoint.",
        scope="private",
        subnet_control="no-default-gateway",
    ).nlb_endpoint(description="NLB endpoint, unicast mode.", mac="00:61:AA:00:00:01", nlb_mode=1)
    epg.subnet(
        "10.61.2.1/32",
        description="Host subnet with an IGMP-mode NLB endpoint.",
        scope="private",
        subnet_control="no-default-gateway",
    ).nlb_endpoint(
        description="NLB endpoint, IGMP mode.",
        mac="01:00:5e:7f:61:02",
        multicast_group_ip_address="239.61.2.3",
        nlb_mode=3,
    )
    epg.subnet(
        "10.61.3.1/32",
        description="Host subnet with an anycast endpoint.",
        scope="private",
        subnet_control="no-default-gateway",
    ).anycast_endpoint("00:61:AA:00:00:04", description="Anycast endpoint.")
    epg.subnet(
        "10.61.4.1/24",
        description="Subnet with a Microsoft endpoint network configuration.",
        scope="private,shared",
    ).endpoint_network_config(
        "niwaki-it-epg-netcfg",
        description="Microsoft client endpoint network configuration.",
        start_ip="10.61.4.10",
        end_ip="10.61.4.20",
        dns_servers="10.61.4.2",
        dns_suffix="corp.local",
    )

    tn.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (f"uni/tn-{TN}",):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
