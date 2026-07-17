"""Tenant — bridge-domain subnet matrix, combination-exhaustive (non-prod).

Run:
    uv run pytest tests/integration/04_tenant/test_006_bd_subnets.py -m integration -s

Subnets across the full scope x control matrix. IPv4 subnets over route scope
(private / public / public+shared / private+shared) x subnet control
(unspecified / querier / no-default-gateway), and IPv6 subnets over the same
scopes x control (unspecified / ND — ND controls are IPv6-only). Public subnets
are advertised out a stub L3Out; ND subnets bind an ND-prefix policy. Anycast
endpoints and the Microsoft endpoint network configuration cover the subnet
children.

Values are illustrative. This file owns tenant ``niwaki-it-bd-sub``; ``wipe``
(operator-only) deletes it.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import tenant
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

TN = "niwaki-it-bd-sub"
VRF = "niwaki-it-bd-sub-vrf"
L3OUT = "niwaki-it-bd-sub-l3out"
NDPFX = "niwaki-it-bd-sub-ndpfx"

SCOPES = ["private", "public", "public,shared", "private,shared"]
IPV4_CTRL = [None, "querier", "no-default-gateway"]
IPV6_CTRL = [None, "nd"]


def test_bd_subnet_matrix(live_aci: Niwaki) -> None:
    """IPv4 and IPv6 subnets across every scope x control combination on one BD."""
    tn = tenant(TN, description="BD subnet scope and control matrix, IPv4 and IPv6, with endpoints")
    tn.vrf(VRF, description="VRF backing the subnet-matrix BD.")
    tn.l3out(L3OUT, description="Stub L3Out anchoring the public subnets.").bind(vrf=VRF)
    tn.nd_ra_prefix_policy(
        NDPFX,
        description="ND RA prefix policy bound onto IPv6 subnets.",
        prefix_controls="auto-cfg,on-link",
        valid_lifetime=2592000,
        preferred_lifetime=604800,
    )

    bd = tn.bd(
        "niwaki-it-bd-subnets",
        description="Dual-stack BD carrying the full subnet matrix.",
        unicast_routing=True,
    ).bind(vrf=VRF, l3out=L3OUT)

    index = 0
    for scope in SCOPES:
        for ctrl in IPV4_CTRL:
            sub = bd.subnet(
                f"10.{index}.0.1/24",
                description=f"IPv4 subnet: scope {scope}, control {ctrl or 'unspecified'}.",
                scope=scope,
                subnet_control=ctrl,
                preferred=(index == 0),
                ip_dp_learning="enabled" if index % 2 else "disabled",
            )
            if "public" in scope:
                sub.bind(l3out=L3OUT)
            index += 1
        for ctrl in IPV6_CTRL:
            sub = bd.subnet(
                f"2001:db8:c{index:02d}::1/64",
                description=f"IPv6 subnet: scope {scope}, control {ctrl or 'unspecified'}.",
                scope=scope,
                subnet_control=ctrl,
            )
            public = "public" in scope
            if public and ctrl == "nd":
                sub.bind(l3out=L3OUT, nd_ra_prefix_policy=NDPFX)
            elif public:
                sub.bind(l3out=L3OUT)
            elif ctrl == "nd":
                sub.bind(nd_ra_prefix_policy=NDPFX)
            index += 1

    tn.push(live_aci)


def test_bd_subnet_children(live_aci: Niwaki) -> None:
    """Anycast endpoint (/32 host subnet) and Microsoft endpoint network config."""
    tn = tenant(TN, description="BD subnet scope and control matrix, IPv4 and IPv6, with endpoints")
    tn.vrf(VRF, description="VRF backing the endpoint-subnet BD.")

    bd = tn.bd(
        "niwaki-it-bd-subnet-eps",
        description="BD whose subnets carry endpoint children.",
        unicast_routing=True,
    ).bind(vrf=VRF)

    bd.subnet(
        "10.200.1.1/32",
        description="Host subnet carrying an anycast endpoint.",
        scope="private",
        subnet_control="no-default-gateway",
    ).anycast_endpoint("00:22:BD:F4:AC:01", description="Anycast endpoint.")
    bd.subnet(
        "10.200.2.1/24",
        description="Subnet carrying a Microsoft endpoint network configuration.",
        scope="private",
    ).endpoint_network_config(
        "niwaki-it-net-cfg",
        description="Microsoft client endpoint network configuration.",
        start_ip="10.200.2.10",
        end_ip="10.200.2.20",
        dns_servers="10.200.2.2",
        dns_suffix="corp.local",
    )

    tn.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (f"uni/tn-{TN}",):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
