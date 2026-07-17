"""Tenant — layer-2 (routing-off) bridge domains, combination-exhaustive (non-prod).

Run:
    uv run pytest tests/integration/04_tenant/test_005_bd_l2.py -m integration -s

One bridge domain (``fvBD``) per valid forwarding combination in the
unicast-routing-off slice (ARP flooding must be on when routing is off):
unknown-unicast action (proxy / flood) x multi-destination action (bd-flood /
drop, plus encap-flood for the flood variant only — hardware proxy excludes it)
x unknown IPv4 multicast (flood / opt-flood) x unknown IPv6 multicast (flood /
opt-flood) x endpoint move-detection (GARP / off). Layer-2 BDs carry no subnets.

Values are illustrative. This file owns tenant ``niwaki-it-bd-l2``; ``wipe``
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

TN = "niwaki-it-bd-l2"
VRF = "niwaki-it-bd-l2-vrf"
MON = "niwaki-it-bd-l2-mon"

V4MC = ["flood", "opt-flood"]
V6MC = ["flood", "opt-flood"]
MOVE = ["garp", ""]
# (unknown_mac, multi_dst) — hardware proxy excludes encap-flood.
UMAC_MULTI = [
    ("proxy", "bd-flood"),
    ("proxy", "drop"),
    ("flood", "bd-flood"),
    ("flood", "drop"),
    ("flood", "encap-flood"),
]


def test_bd_l2_matrix(live_aci: Niwaki) -> None:
    """A routing-off BD per forwarding combination (ARP flooding on)."""
    tn = tenant(TN, description="BD forwarding cartesian - layer-2 routing-off variants")
    tn.vrf(VRF, description="VRF backing the layer-2 BDs.")
    tn.monitoring_policy(MON, description="Monitoring policy bound onto the BDs.")

    for index, ((umac, multi), v4, v6, move) in enumerate(
        itertools.product(UMAC_MULTI, V4MC, V6MC, MOVE)
    ):
        tn.bd(
            f"niwaki-it-bd-l2{index:02d}",
            description=f"Layer-2 BD: {umac}, {multi}, v4 {v4}, v6 {v6}.",
            unicast_routing=False,
            arp_flooding=True,
            unknown_mac_unicast_action=umac,
            multi_destination_packet_action=multi,
            unknown_multicast_destination_action=v4,
            unknown_v6_multicast_destination_action=v6,
            ep_move_detection_mode=move,
        ).bind(vrf=VRF, monitoring_policy=MON)

    tn.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (f"uni/tn-{TN}",):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
