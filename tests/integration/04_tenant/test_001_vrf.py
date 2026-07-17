"""Tenant — VRF matrix, combination-exhaustive (non-prod).

Run:
    uv run pytest tests/integration/04_tenant/test_001_vrf.py -m integration -s

A VRF (``fvCtx``) per combination of enforcement preference x enforcement
direction x data-plane learning x known-multicast action — the full cartesian —
each binding a variant of every protocol policy (BGP timers / address-family,
EIGRP address-family, OSPF timers, endpoint retention, route-tag, VRF
validation, monitoring) and carrying the simple VRF children (DNS labels, SNMP
context, global name, route summarization, route deployment, BGP route targets).

Values are illustrative — they exercise the SDK surface, not a routing design.
This file owns tenant ``niwaki-it-vrf``; ``wipe`` (operator-only) deletes it.
"""

from __future__ import annotations

import contextlib
import itertools

import pytest

from niwaki import Niwaki
from niwaki.design import tenant
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

TN = "niwaki-it-vrf"

ENFORCE = ["enforced", "unenforced"]
DIRECTION = ["ingress", "egress"]  # "mixed" is controller-derived, not settable
LEARNING = ["enabled", "disabled"]
KNOWN_MCAST = ["permit", "deny"]


def _policies(tn):  # type: ignore[no-untyped-def]
    """Create every VRF bind-target policy in variants covering its enums."""
    tn.bgp_timers_policy(
        "niwaki-it-bgp-gr",
        description="BGP timers with graceful-restart helper.",
        graceful_restart_controls="helper",
        hold_interval=180,
        keepalive_interval=60,
        stale_interval=300,
    )
    tn.bgp_timers_policy(
        "niwaki-it-bgp-plain",
        description="BGP timers without graceful restart.",
        graceful_restart_controls="",
        hold_interval=90,
        keepalive_interval=30,
    )
    tn.bgp_address_family_context_policy(
        "niwaki-it-bgp-af-leak",
        description="BGP AF context with host-route leaking.",
        controls="host-rt-leak",
        ebgp_distance=20,
        ibgp_distance=200,
        local_distance=220,
    )
    tn.bgp_address_family_context_policy(
        "niwaki-it-bgp-af-plain",
        description="BGP AF context without host-route leaking.",
        controls="",
        ebgp_distance=25,
    )
    tn.eigrp_address_family_context_policy(
        "niwaki-it-eigrp-narrow",
        description="EIGRP AF context, narrow metrics.",
        metric_style="narrow",
        external_distance=170,
        internal_distance=90,
    )
    tn.eigrp_address_family_context_policy(
        "niwaki-it-eigrp-wide",
        description="EIGRP AF context, wide metrics.",
        metric_style="wide",
        maximum_ecmp_paths=8,
    )
    tn.ospf_timers_policy(
        "niwaki-it-ospf-suppress",
        description="OSPF timers, prefix-suppression, reject on max-LSA.",
        control_knobs="pfx-suppress",
        graceful_restart_controls="helper",
        action="reject",
    )
    tn.ospf_timers_policy(
        "niwaki-it-ospf-both",
        description="OSPF timers, both domain controls, log on max-LSA.",
        control_knobs="pfx-suppress,name-lookup",
        action="log",
    )
    tn.ep_retention_policy(
        "niwaki-it-epret-proto",
        description="Endpoint retention, protocol bounce trigger.",
        ep_bounce_trigger="protocol",
        ep_hold_interval=300,
    )
    tn.ep_retention_policy(
        "niwaki-it-epret-rarp",
        description="Endpoint retention, RARP-flood bounce trigger.",
        ep_bounce_trigger="rarp-flood",
        remote_ep_age_interval=300,
    )
    tn.route_tag_policy(
        "niwaki-it-route-tag", description="Route-tag policy.", route_tag=4294967295
    )
    tn.vrf_validation_policy(
        "niwaki-it-vrf-val",
        description="VRF validation, several validators enabled.",
        enable_bgpinfrapeer_policy_validation=True,
        enable_vrf_validation_ip_address=True,
        enable_subnet_non_duplication_validation=True,
    )
    tn.monitoring_policy("niwaki-it-mon", description="Monitoring policy.")


def test_vrf_matrix(live_aci: Niwaki) -> None:
    """A VRF per enforcement x direction x learning x known-multicast combination."""
    tn = tenant(
        TN,
        description="VRF enforcement/direction/learning/known-mcast matrix plus protocol binds",
    )
    _policies(tn)

    for index, (enforce, direction, learning, kmc) in enumerate(
        itertools.product(ENFORCE, DIRECTION, LEARNING, KNOWN_MCAST)
    ):
        odd = index % 2
        vrf = tn.vrf(
            f"niwaki-it-vrf-{index:02d}",
            description=f"VRF: {enforce}, {direction}, learning {learning}, mcast {kmc}.",
            policy_control_enforcement=enforce,
            policy_enforcement_direction=direction,
            data_plane_learning=learning,
            known_multicast_action=kmc,
            bd_enforcement_status=bool(odd),
            vrf_index=index + 1,
        )
        vrf.bind(
            bgp_timers="niwaki-it-bgp-gr" if odd else "niwaki-it-bgp-plain",
            bgp_address_family="niwaki-it-bgp-af-leak" if odd else "niwaki-it-bgp-af-plain",
            eigrp_address_family="niwaki-it-eigrp-wide" if odd else "niwaki-it-eigrp-narrow",
            endpoint_retention="niwaki-it-epret-proto" if odd else "niwaki-it-epret-rarp",
            route_tag="niwaki-it-route-tag",
            vrf_validation="niwaki-it-vrf-val",
            monitoring_policy="niwaki-it-mon",
        )
        vrf.ospf_timers("niwaki-it-ospf-suppress" if odd else "niwaki-it-ospf-both")

    tn.push(live_aci)


def test_vrf_children(live_aci: Niwaki) -> None:
    """The simple VRF children, covering every enum value per child."""
    tn = tenant(
        TN,
        description="VRF enforcement/direction/learning/known-mcast matrix plus protocol binds",
    )

    vrf = tn.vrf("niwaki-it-vrf-children", description="VRF exercising its simple children.")
    vrf.dns_label("niwaki-it-dns-blue", description="DNS label, blue.", tag="blue")
    vrf.dns_label("niwaki-it-dns-green", description="DNS label, green.", tag="green")
    vrf.global_vrf_name(name="niwaki-it-global-name", description="Global VRF name.")
    vrf.snmp_context(name="niwaki-it-snmp-ctx")
    vrf.route_summarization("niwaki-it-rtsumm", description="VRF route summarization policy.")
    vrf.route_deployment(
        description="Route deployment, contract spine intra-VRF.", spine_intra_vrf="contract"
    )
    vrf.route_target_profile(
        "ipv4-ucast", name="niwaki-it-rt-v4", description="BGP route targets, IPv4 unicast."
    )
    vrf.route_target_profile(
        "ipv6-ucast", name="niwaki-it-rt-v6", description="BGP route targets, IPv6 unicast."
    )
    tn.vrf(
        "niwaki-it-vrf-children-auto", description="VRF with automatic route deployment."
    ).route_deployment(
        description="Route deployment, automatic spine intra-VRF.", spine_intra_vrf="automatic"
    )

    tn.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (f"uni/tn-{TN}",):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
