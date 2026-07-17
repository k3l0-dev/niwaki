"""Tenant contracts — ether-type, IP-protocol, ARP and DSCP filter entries (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/05_contracts/test_003_filters_ethertype.py -m integration -s

The L2/L3 classification slice: one ``vzEntry`` for every ether-type the schema
offers, every ARP opcode, every non-L4 IP protocol (EGP, EIGRP, IGMP, IGP, L2TP,
OSPF, PIM), and every DSCP match value. The apply-to-fragment bit is toggled on the
port-less IP entries (legal without an L4 port).

Values are illustrative — this proves the SDK expresses the L2/L3 filter surface, not
a production ACL. ``wipe(aci)`` (operator-only) removes what this file owns.
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

FLT_ETHERTYPES = "niwaki-it-flt-ethertypes"
FLT_ETHER_WILDCARD = "niwaki-it-flt-ether-any"
FLT_ARP = "niwaki-it-flt-arp"
FLT_IP_PROTO = "niwaki-it-flt-ip-proto"
FLT_DSCP = "niwaki-it-flt-dscp"

# Every specific L2EtherType (ARP has its own filter; the unspecified wildcard has
# its own filter — the APIC forbids a wildcard entry beside non-wildcard ones).
ETHER_TYPES = ("fcoe", "ip", "ipv4", "ipv6", "mac_security", "mpls_ucast", "trill")
ARP_OPCODES = ("reply", "req", "unspecified")
# Non-L4 IP protocols carried over the generic IP ether-type.
IP_PROTOCOLS = ("egp", "eigrp", "igmp", "igp", "l2tp", "ospfigp", "pim")
# Every DSCP match value (vzEntry matchDscp validValues).
DSCP_VALUES = (
    "AF11", "AF12", "AF13", "AF21", "AF22", "AF23", "AF31", "AF32", "AF33", "AF41", "AF42", "AF43",
    "CS0", "CS1", "CS2", "CS3", "CS4", "CS5", "CS6", "CS7", "EF", "VA", "unspecified",
)  # fmt: skip


def test_ether_types(live_aci: Niwaki) -> None:
    cfg = tenant(TN, description=TN_DESC)
    flt = cfg.filter(FLT_ETHERTYPES, description="One entry per specific ether-type.")
    for et in ETHER_TYPES:
        flt.entry(f"eth-{et}", ethernet_type=et, description=f"Ether-type {et}.")
    # The unspecified (wildcard) ether-type must sit alone in its own filter.
    wild = cfg.filter(FLT_ETHER_WILDCARD, description="Wildcard (unspecified) ether-type entry.")
    wild.entry("eth-any", ethernet_type="unspecified", description="Wildcard ether-type.")
    cfg.push(live_aci)


def test_arp_opcodes(live_aci: Niwaki) -> None:
    cfg = tenant(TN, description=TN_DESC)
    flt = cfg.filter(FLT_ARP, description="ARP entries, one per opcode.")
    for op in ARP_OPCODES:
        flt.entry(f"arp-{op}", ethernet_type="arp", arp_opcodes=op, description=f"ARP opcode {op}.")
    cfg.push(live_aci)


def test_ip_protocols(live_aci: Niwaki) -> None:
    cfg = tenant(TN, description=TN_DESC)
    flt = cfg.filter(FLT_IP_PROTO, description="One entry per non-L4 IP protocol.")
    for i, proto in enumerate(IP_PROTOCOLS):
        flt.entry(
            f"proto-{proto}",
            ethernet_type="ip",
            protocol=proto,
            apply_to_frag=bool(i % 2),
            description=f"IP protocol {proto} (fragments {bool(i % 2)}).",
        )
    cfg.push(live_aci)


def test_match_dscp(live_aci: Niwaki) -> None:
    cfg = tenant(TN, description=TN_DESC)
    flt = cfg.filter(FLT_DSCP, description="One IP entry per DSCP match value.")
    for dscp in DSCP_VALUES:
        flt.entry(
            f"dscp-{dscp.lower()}",
            ethernet_type="ip",
            match_dscp=dscp,
            description=f"Match DSCP {dscp}.",
        )
    cfg.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (
        f"uni/tn-{TN}/flt-{FLT_ETHERTYPES}",
        f"uni/tn-{TN}/flt-{FLT_ETHER_WILDCARD}",
        f"uni/tn-{TN}/flt-{FLT_ARP}",
        f"uni/tn-{TN}/flt-{FLT_IP_PROTO}",
        f"uni/tn-{TN}/flt-{FLT_DSCP}",
    ):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
