"""Tenant contracts — ICMPv4 / ICMPv6 filter entries, every named type (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/05_contracts/test_002_filters_icmp.py -m integration -s

The ICMP slice of the filter surface: one ``vzEntry`` for every named ICMPv4 type and
every named ICMPv6 type the schema offers, plus the apply-to-fragment bit toggled
across them (ICMP entries carry no L4 port, so the all-fragments rule is legal here).
The ether-type default follows the family through the entry sugar (ip for v4, ipv6
for v6).

Values are illustrative — this proves the SDK expresses the ICMP filter surface, not a
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

FLT_ICMP4 = "niwaki-it-flt-icmp4"
FLT_ICMP6 = "niwaki-it-flt-icmp6"

# Every named ICMPv4 / ICMPv6 type from the vzEntry schema (validValues).
ICMP4_TYPES = ("dst-unreach", "echo", "echo-rep", "src-quench", "time-exceeded", "unspecified")
ICMP6_TYPES = (
    "dst-unreach",
    "echo-rep",
    "echo-req",
    "nbr-advert",
    "nbr-solicit",
    "redirect",
    "time-exceeded",
    "unspecified",
)


def test_icmpv4_types(live_aci: Niwaki) -> None:
    cfg = tenant(TN, description=TN_DESC)
    flt = cfg.filter(FLT_ICMP4, description="ICMPv4 entries, one per named type.")
    for i, t in enumerate(ICMP4_TYPES):
        flt.entry(
            f"icmp4-{t}",
            protocol="icmp",
            icmpv4_type=t,
            apply_to_frag=bool(i % 2),
            description=f"ICMPv4 type {t} (fragments {bool(i % 2)}).",
        )
    cfg.push(live_aci)


def test_icmpv6_types(live_aci: Niwaki) -> None:
    cfg = tenant(TN, description=TN_DESC)
    flt = cfg.filter(FLT_ICMP6, description="ICMPv6 entries, one per named type.")
    for i, t in enumerate(ICMP6_TYPES):
        flt.entry(
            f"icmp6-{t}",
            protocol="icmpv6",
            icmpv6_type=t,
            apply_to_frag=bool(i % 2),
            description=f"ICMPv6 type {t} (fragments {bool(i % 2)}).",
        )
    cfg.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (
        f"uni/tn-{TN}/flt-{FLT_ICMP4}",
        f"uni/tn-{TN}/flt-{FLT_ICMP6}",
    ):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
