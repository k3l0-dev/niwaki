"""Tenant — routed flood bridge domains, combination-exhaustive (non-prod).

Run:
    uv run pytest tests/integration/04_tenant/test_004_bd_routed_flood.py -m integration -s

One bridge domain (``fvBD``) per valid forwarding combination in the
flood-unknown-unicast, unicast-routing-on slice — the full cartesian of ARP
flooding x multi-destination action (bd-flood / drop / encap-flood) x unknown
IPv4 multicast x unknown IPv6 multicast x endpoint move-detection x
IPv6-multicast-allow x limit-IP-learn-to-subnets. That is 192 bridge domains,
spread across four VRFs (one per test function). Each BD binds its VRF and a
monitoring policy and carries four subnets (IPv4/IPv6 x private/private-shared).

Values are illustrative. This file owns tenant ``niwaki-it-bd-flood``; ``wipe``
(operator-only) deletes it.
"""

from __future__ import annotations

import contextlib
import itertools

import pytest

from niwaki import Niwaki
from niwaki.design import tenant
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

TN = "niwaki-it-bd-flood"
TN_DESC = "BD forwarding cartesian - flood unknown-unicast variants"
MON = "niwaki-it-bd-flood-mon"

ARP = [True, False]
MULTI = ["bd-flood", "drop", "encap-flood"]
V4MC = ["flood", "opt-flood"]
V6MC = ["flood", "opt-flood"]
MOVE = ["garp", ""]
IPV6MC = [True, False]
LIMIT = [True, False]
COMBOS = list(itertools.product(ARP, MULTI, V4MC, V6MC, MOVE, IPV6MC, LIMIT))  # 192


def _build(tn, vrf, combos, offset):  # type: ignore[no-untyped-def]
    """Create a VRF, a monitoring policy and one BD per combination with subnets."""
    tn.vrf(vrf, description="VRF for a slice of the flood BD matrix.")
    tn.monitoring_policy(MON, description="Monitoring policy bound onto the BDs.")
    for i, (arp, multi, v4, v6, move, ipv6mc, limit) in enumerate(combos):
        idx = offset + i
        bd = tn.bd(
            f"niwaki-it-bd-f{idx:03d}",
            description=f"Routed flood BD: arp {arp}, {multi}, v4 {v4}, v6 {v6}.",
            unicast_routing=True,
            unknown_mac_unicast_action="flood",
            arp_flooding=arp,
            multi_destination_packet_action=multi,
            unknown_multicast_destination_action=v4,
            unknown_v6_multicast_destination_action=v6,
            ep_move_detection_mode=move,
            ipv6_multicast_allow=ipv6mc,
            limit_ip_learn_to_subnets=limit,
        ).bind(vrf=vrf, monitoring_policy=MON)
        bd.subnet(
            f"10.{idx // 4}.{idx % 4}.1/24", description="IPv4 private gateway.", scope="private"
        )
        bd.subnet(
            f"10.{100 + idx // 4}.{idx % 4}.1/24",
            description="IPv4 private-shared gateway.",
            scope="private,shared",
        )
        bd.subnet(
            f"2001:db8:f{idx:03x}::1/64", description="IPv6 private gateway.", scope="private"
        )
        bd.subnet(
            f"2001:db8:f{idx:03x}:1::1/64",
            description="IPv6 private-shared gateway.",
            scope="private,shared",
        )


def test_bd_flood_slice_a(live_aci: Niwaki) -> None:
    """First slice of the flood BD matrix."""
    tn = tenant(TN, description=TN_DESC)
    _build(tn, "niwaki-it-bd-flood-vrf-a", COMBOS[0:48], 0)
    tn.push(live_aci)


def test_bd_flood_slice_b(live_aci: Niwaki) -> None:
    """Second slice of the flood BD matrix."""
    tn = tenant(TN, description=TN_DESC)
    _build(tn, "niwaki-it-bd-flood-vrf-b", COMBOS[48:96], 48)
    tn.push(live_aci)


def test_bd_flood_slice_c(live_aci: Niwaki) -> None:
    """Third slice of the flood BD matrix."""
    tn = tenant(TN, description=TN_DESC)
    _build(tn, "niwaki-it-bd-flood-vrf-c", COMBOS[96:144], 96)
    tn.push(live_aci)


def test_bd_flood_slice_d(live_aci: Niwaki) -> None:
    """Fourth slice of the flood BD matrix."""
    tn = tenant(TN, description=TN_DESC)
    _build(tn, "niwaki-it-bd-flood-vrf-d", COMBOS[144:192], 144)
    tn.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (f"uni/tn-{TN}",):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
