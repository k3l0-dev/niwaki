"""Tenant contracts — port-zero filter entries, full cartesian (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/05_contracts/test_004_filters_portzero.py -m integration -s

The ``vzEntryPortZero`` variant, driven across its whole knob set: the cartesian of
direction (both / source / destination) x ether-type (ip / ipv4 / ipv6) x protocol
(sctp / tcp / udp), the all-fragments and stateful bits both ways, a rotating DSCP
match, and — on the TCP rows — every session-flag combination.

Values are illustrative — this proves the SDK expresses the port-zero surface, not a
production ACL. ``wipe(aci)`` (operator-only) removes what this file owns.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import tenant
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

TN = "niwaki-it-contracts"
TN_DESC = "Exhaustive contract, filter, label, taboo, vzAny and QoS surface"

FLT_PZ = "niwaki-it-flt-pz"
FLT_PZ_FLAGS = "niwaki-it-flt-pz-flags"

DIRECTIONS = ("both", "source", "destination")
PZ_ETHER_TYPES = ("ip", "ipv4", "ipv6")
PZ_PROTOCOLS = ("sctp", "tcp", "udp")
PZ_DSCP = ("CS0", "CS1", "CS2", "CS3", "CS4", "CS5", "CS6", "CS7", "EF", "unspecified")
TCP_FLAG_COMBOS = ("unspecified", "syn", "ack", "fin", "rst", "est", "ack,syn", "ack,fin,rst,syn")


def test_portzero_cartesian(live_aci: Niwaki) -> None:
    # Every (direction x ether-type x protocol) combination; the all-fragments and
    # stateful bits alternate, DSCP rotates, and TCP rows carry session flags
    # (tcp_rules / stateful apply to TCP only).
    cfg = tenant(TN, description=TN_DESC)
    flt = cfg.filter(FLT_PZ, description="Port-zero entries across direction/ether/protocol.")
    n = 0
    for direction in DIRECTIONS:
        for ether in PZ_ETHER_TYPES:
            for proto in PZ_PROTOCOLS:
                name = f"pz-{direction}-{ether}-{proto}"
                frag = bool(n % 2)
                dscp = PZ_DSCP[n % len(PZ_DSCP)]
                desc = f"Port-zero {direction}/{ether}/{proto}."
                if proto == "tcp":
                    flt.port_zero_entry(
                        name,
                        port_zero_direction=direction,
                        ethernet_type=ether,
                        protocol=proto,
                        apply_rule_for_all_fragments=frag,
                        dscp_match_for_filter_entry=dscp,
                        tcp_rules=TCP_FLAG_COMBOS[n % len(TCP_FLAG_COMBOS)],
                        stateful=frag,
                        description=desc,
                    )
                else:
                    flt.port_zero_entry(
                        name,
                        port_zero_direction=direction,
                        ethernet_type=ether,
                        protocol=proto,
                        apply_rule_for_all_fragments=frag,
                        dscp_match_for_filter_entry=dscp,
                        description=desc,
                    )
                n += 1
    cfg.push(live_aci)


def test_portzero_tcp_flags(live_aci: Niwaki) -> None:
    # Every TCP session-flag combination on a port-zero TCP entry.
    cfg = tenant(TN, description=TN_DESC)
    flt = cfg.filter(FLT_PZ_FLAGS, description="Port-zero TCP entries, one per flag combo.")
    for i, combo in enumerate(TCP_FLAG_COMBOS):
        flt.port_zero_entry(
            f"pz-flags-{i:02d}",
            port_zero_direction="both",
            ethernet_type="ipv4",
            protocol="tcp",
            tcp_rules=combo,
            stateful=bool(i % 2),
            description=f"Port-zero TCP matching flags ({combo}).",
        )
    cfg.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (
        f"uni/tn-{TN}/flt-{FLT_PZ}",
        f"uni/tn-{TN}/flt-{FLT_PZ_FLAGS}",
    ):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
