"""Tenant contracts — L4 (TCP/UDP) filter entries, full cartesian (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/05_contracts/test_001_filters_l4.py -m integration -s

The layer-4 slice of the filter surface, driven to the corners. Every TCP session
flag and representative combination (empty, single, several), both statefulness
values, named ports and numeric ports, source-and-destination port ranges, and the
``tcp=`` / ``udp=`` shorthands in all their spellings (named, numeric, dashed range,
tuple range). One ``vzEntry`` per combination, grouped into a handful of filters.

Values are illustrative — this proves the SDK expresses the L4 filter surface, not a
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

FLT_TCP_PORTS = "niwaki-it-flt-tcp-ports"
FLT_UDP_PORTS = "niwaki-it-flt-udp-ports"
FLT_TCP_FLAGS = "niwaki-it-flt-tcp-flags"
FLT_L4_RANGES = "niwaki-it-flt-l4-ranges"
FLT_L4_FRAG = "niwaki-it-flt-l4-frag"

# Named destination ports the APIC knows (stored by name on the wire).
NAMED_PORTS = ("dns", "ftpData", "http", "https", "pop3", "rtsp", "smtp", "ssh")

# Every TCP session-flag value (L4TcpFlags) and representative Flags combinations:
# empty (unspecified), each single flag, and several multi-flag sets. `est`
# (established) is only valid on its own — the APIC rejects it combined with others.
TCP_FLAG_COMBOS = (
    "unspecified",
    "syn",
    "ack",
    "fin",
    "rst",
    "est",
    "ack,syn",
    "fin,rst",
    "ack,fin,rst",
    "ack,fin,rst,syn",
)


def test_tcp_named_ports(live_aci: Niwaki) -> None:
    # Each named port as a TCP destination, alternating the stateful bit so both
    # boolean values are exercised.
    cfg = tenant(TN, description=TN_DESC)
    flt = cfg.filter(FLT_TCP_PORTS, description="TCP entries, one per named port.")
    for i, port in enumerate(NAMED_PORTS):
        flt.entry(
            f"tcp-{port}",
            tcp=port,
            stateful=bool(i % 2),
            description=f"TCP to {port} (stateful {bool(i % 2)}).",
        )
    cfg.push(live_aci)


def test_udp_named_ports(live_aci: Niwaki) -> None:
    # The same named ports as UDP destinations (the names are just port numbers).
    cfg = tenant(TN, description=TN_DESC)
    flt = cfg.filter(FLT_UDP_PORTS, description="UDP entries, one per named port.")
    for port in NAMED_PORTS:
        flt.entry(f"udp-{port}", udp=port, description=f"UDP to {port}.")
    cfg.push(live_aci)


def test_tcp_session_flags(live_aci: Niwaki) -> None:
    # Every TCP flag combination on an otherwise-identical HTTPS entry.
    cfg = tenant(TN, description=TN_DESC)
    flt = cfg.filter(FLT_TCP_FLAGS, description="TCP entries covering every session-flag combo.")
    for i, combo in enumerate(TCP_FLAG_COMBOS):
        flt.entry(
            f"tcp-flags-{i:02d}",
            tcp=443,
            tcp_rules=combo,
            stateful=bool(i % 2),
            description=f"TCP/443 matching flags ({combo}).",
        )
    cfg.push(live_aci)


def test_l4_ranges_and_source_ports(live_aci: Niwaki) -> None:
    # Numeric singles, tuple ranges, dashed-string ranges, and explicit
    # source+destination port windows.
    cfg = tenant(TN, description=TN_DESC)
    flt = cfg.filter(FLT_L4_RANGES, description="TCP/UDP numeric singles, ranges and source ports.")
    flt.entry("tcp-single-8080", tcp=8080, description="TCP single numeric port.")
    flt.entry("udp-single-4789", udp=4789, description="UDP single numeric port (VXLAN).")
    flt.entry("tcp-tuple-range", tcp=(1024, 2048), description="TCP tuple range.")
    flt.entry("udp-tuple-range", udp=(5000, 6000), description="UDP tuple range.")
    flt.entry("tcp-dashed-range", tcp="3000-3100", description="TCP dashed-string range.")
    flt.entry("tcp-full-range", tcp=(1, 65535), description="TCP wide-port range.")
    flt.entry(
        "tcp-src-dst",
        ethernet_type="ip",
        protocol="tcp",
        source_from_port=1024,
        source_to_port=65535,
        destination_from_port="http",
        destination_to_port="https",
        stateful=True,
        description="Explicit TCP source window to a destination port range.",
    )
    flt.entry(
        "udp-src-dst",
        ethernet_type="ip",
        protocol="udp",
        source_from_port=32768,
        source_to_port=60999,
        destination_from_port="dns",
        destination_to_port="dns",
        description="Explicit UDP source window to the DNS port.",
    )
    cfg.push(live_aci)


def test_l4_fragment_entries(live_aci: Niwaki) -> None:
    # The other side of the apply-to-fragment factoring: an L4 protocol entry either
    # carries a port (apply_to_frag=False, above) or applies to all fragments with no
    # port. These are the port-less fragment entries for TCP, UDP and any-IP.
    cfg = tenant(TN, description=TN_DESC)
    flt = cfg.filter(FLT_L4_FRAG, description="Port-less all-fragment L4 entries.")
    flt.entry(
        "frag-tcp",
        ethernet_type="ip",
        protocol="tcp",
        apply_to_frag=True,
        description="TCP applied to all fragments (no port).",
    )
    flt.entry(
        "frag-udp",
        ethernet_type="ip",
        protocol="udp",
        apply_to_frag=True,
        description="UDP applied to all fragments (no port).",
    )
    flt.entry(
        "frag-ip",
        ethernet_type="ip",
        apply_to_frag=True,
        description="Any IP applied to all fragments.",
    )
    cfg.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (
        f"uni/tn-{TN}/flt-{FLT_TCP_PORTS}",
        f"uni/tn-{TN}/flt-{FLT_UDP_PORTS}",
        f"uni/tn-{TN}/flt-{FLT_TCP_FLAGS}",
        f"uni/tn-{TN}/flt-{FLT_L4_RANGES}",
        f"uni/tn-{TN}/flt-{FLT_L4_FRAG}",
    ):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
