"""Fabric access — per-interface CoPP policies (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/02_fabric-access/test_015_copp.py -m integration -s

The control-plane-policing shelf: a per-interface CoPP policy carrying one
protocol class per protocol (every CoPP protocol value exercised, with a spread of
rate/burst), and a second policy carrying multi-protocol match combinations.
Values are illustrative and cover the SDK surface, not a real CoPP plan.

This file owns only its niwaki-it-* policies; wipe(aci) removes them and is run by
hand (never by the suite).
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import Cursor, infra
from niwaki.exceptions import NotFoundError
from niwaki.models._generated.aaa.aaaRbacAnnotation import aaaRbacAnnotation
from niwaki.models._generated.tag.tagAnnotation import tagAnnotation
from niwaki.models._generated.tag.tagTag import tagTag

pytestmark = pytest.mark.integration

PROTOCOLS = ("bgp", "ospf", "cdp", "lldp", "lacp", "arp", "icmp", "stp", "bfd")
# Multi-protocol match combinations (CoppProtocol flags). Within one CoPP policy a
# protocol may appear in only one class, so the combos partition the protocol set.
COMBOS: tuple[tuple[str, str], ...] = (
    ("bgp-ospf", "bgp,ospf"),
    ("arp-icmp", "arp,icmp"),
    ("l2ctrl", "cdp,lldp,lacp,stp,bfd"),
)

COPP_PER_PROTO = "niwaki-it-copp-per-proto"
COPP_COMBOS = "niwaki-it-copp-combos"
COPP_ALL = "niwaki-it-copp-all"
ALL_PROTOCOLS = ",".join(PROTOCOLS)


def _common(obj: Cursor) -> None:
    """Attach the universal children (annotation, tag, RBAC domain marker)."""
    obj.mo(tagAnnotation, key="orchestrator", value="niwaki-it")
    obj.mo(tagTag, key="lifecycle", value="integration")
    obj.mo(aaaRbacAnnotation, domain="all")


def test_copp_per_protocol(live_aci: Niwaki) -> None:
    """A CoPP interface policy with one class per protocol."""
    fab = infra()
    copp = fab.copp_interface_policy(
        COPP_PER_PROTO,
        description="Per-interface CoPP - one class per protocol.",
    )
    _common(copp)
    for idx, proto in enumerate(PROTOCOLS):
        cls = copp.protocol_class(
            f"cls-{proto}",
            match_proto=proto,
            rate=(50, 100, 200, 300, 500)[idx % 5],
            burst=(25, 50, 100, 150, 250)[idx % 5],
            description=f"CoPP class - {proto}.",
        )
        _common(cls)
    fab.push(live_aci)


def test_copp_combos(live_aci: Niwaki) -> None:
    """A CoPP interface policy with multi-protocol match combinations."""
    fab = infra()
    copp = fab.copp_interface_policy(
        COPP_COMBOS,
        description="Per-interface CoPP - partitioned multi-protocol classes.",
    )
    _common(copp)
    for slug, protos in COMBOS:
        cls = copp.protocol_class(
            f"cls-{slug}",
            match_proto=protos,
            rate=400,
            burst=200,
            description=f"CoPP class - protocols ({protos}).",
        )
        _common(cls)
    fab.push(live_aci)


def test_copp_all_protocols(live_aci: Niwaki) -> None:
    """A CoPP interface policy with a single class matching every protocol.

    A protocol may appear in only one class per policy, so the all-protocol match
    is valid as one class in its own policy (it cannot coexist with the per-protocol
    classes of ``test_copp_per_protocol``).
    """
    fab = infra()
    copp = fab.copp_interface_policy(
        COPP_ALL,
        description="Per-interface CoPP - single all-protocol class.",
    )
    _common(copp)
    cls = copp.protocol_class(
        "cls-all",
        match_proto=ALL_PROTOCOLS,
        rate=1000,
        burst=500,
        description="CoPP class - all protocols in one class.",
    )
    _common(cls)
    fab.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for name in (COPP_PER_PROTO, COPP_COMBOS, COPP_ALL):
        with contextlib.suppress(NotFoundError):
            aci.node(f"uni/infra/coppifpol-{name}").delete()
