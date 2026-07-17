"""Tenant — VRF multicast and route leaking, combination-exhaustive (non-prod).

Run:
    uv run pytest tests/integration/04_tenant/test_002_vrf_multicast.py -m integration -s

VRF-level multicast and inter-VRF leaking. PIM (IPv4) and PIM6 (IPv6) with their
domain control flags empty / one / several, every rendezvous-point mechanism
(static, auto, bootstrap, fabric RP) with per-mechanism control flags, the
ASM/SSM patterns, resource, inter-VRF and stripe-winner policies; IGMP with SSM
translation; and inter-VRF route leaking (leaked prefixes with length bounds,
leaked subnets across both visibility scopes) with a fallback-route group.

Values are illustrative. This file owns tenant ``niwaki-it-vrf-mcast``; ``wipe``
(operator-only) deletes it.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import tenant
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

TN = "niwaki-it-vrf-mcast"


def test_vrf_multicast_full(live_aci: Niwaki) -> None:
    """A VRF whose PIM/PIM6 carry every RP mechanism and control-flag variant."""
    tn = tenant(TN, description="VRF multicast PIM/PIM6/IGMP plus inter-VRF route leaking")

    vrf = tn.vrf(
        "niwaki-it-vrf-mcast-full",
        description="VRF hosting the full multicast stack.",
        known_multicast_action="permit",
    )
    vrf.igmp(description="VRF-level IGMP policy.").ssm_translate(
        "232.0.0.0/8", "10.90.0.1", description="IGMP SSM translation."
    )
    pim = vrf.pim(
        description="VRF PIM (IPv4), both domain controls.",
        control_knobs="fast-conv,strict-rfc-compliant",
        mtu=1500,
    )
    pim.static_rp(description="Static RP mechanism.")
    pim.auto_rp(description="Auto-RP, listen and forward.", control_knobs="listen,forward")
    pim.bootstrap_rp(description="Bootstrap RP, listen.", control_knobs="listen")
    pim.fabric_rp(description="Fabric RP mechanism.")
    pim.asm_pattern(description="ASM pattern, pre-build SPT.", control_knobs="pre-build-spt")
    pim.ssm_pattern(description="SSM pattern policy.")
    pim.resource(description="PIM resource policy.", max_entries=4000)
    pim.inter_vrf(description="Inter-VRF PIM policy.")
    pim.stripe_winner(description="Configured stripe-winner policy.")
    vrf.pim6(description="VRF PIM (IPv6), fast-convergence.", control_knobs="fast-conv").static_rp(
        description="IPv6 static RP mechanism."
    )

    tn.push(live_aci)


def test_vrf_multicast_minimal(live_aci: Niwaki) -> None:
    """A second VRF covering the empty control-flag and the other RP flag values."""
    tn = tenant(TN, description="VRF multicast PIM/PIM6/IGMP plus inter-VRF route leaking")
    vrf = tn.vrf(
        "niwaki-it-vrf-mcast-min",
        description="VRF with minimal PIM (empty controls).",
        known_multicast_action="permit",
    )
    pim = vrf.pim(description="VRF PIM with no domain controls.", control_knobs="")
    pim.bootstrap_rp(description="Bootstrap RP, forward.", control_knobs="forward")
    pim.auto_rp(description="Auto-RP, forward only.", control_knobs="forward")
    vrf.pim6(description="VRF PIM6 with no domain controls.", control_knobs="")

    tn.push(live_aci)


def test_vrf_route_leaking(live_aci: Niwaki) -> None:
    """Inter-VRF leaked prefixes/subnets (both visibilities) and a fallback group."""
    tn = tenant(TN, description="VRF multicast PIM/PIM6/IGMP plus inter-VRF route leaking")

    leak = tn.vrf(
        "niwaki-it-vrf-leak", description="VRF carrying inter-VRF leaked routes."
    ).leak_routes(description="Inter-VRF leaked-routes container.")
    leak.internal_prefix(
        "10.50.0.0/16",
        description="Internal leaked prefix with length bounds.",
        greater_then=24,
        less_than_or_equal=32,
    )
    leak.internal_prefix("10.51.0.0/16", description="Internal leaked prefix, no length bounds.")
    leak.external_prefix(
        "192.0.2.0/24",
        description="External leaked prefix with length bounds.",
        greater_then=25,
        less_than_or_equal=32,
    )
    leak.internal_subnet(
        "10.60.0.0/16",
        description="Leaked subnet, private visibility.",
        visibility_of_the_subnet="private",
    )
    leak.internal_subnet(
        "10.61.0.0/16",
        description="Leaked subnet, public visibility.",
        visibility_of_the_subnet="public",
    )

    fbr = tn.vrf(
        "niwaki-it-vrf-fallback", description="VRF carrying a fallback-route group."
    ).fallback_route_group("niwaki-it-fbr", description="Fallback-route group.")
    fbr.fallback_route("0.0.0.0/0", description="Default fallback route.")
    fbr.fallback_member("10.70.0.1", description="Fallback next-hop member.")
    fbr.fallback_member("10.70.0.2", description="Second fallback next-hop member.")

    tn.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (f"uni/tn-{TN}",):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
