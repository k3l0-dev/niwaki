"""Fabric access — encapsulation pools (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/02_fabric-access/test_002_pools.py -m integration -s

The operator lays down the encapsulation namespaces the access domains draw from:
VLAN, VXLAN, VSAN and multicast-address pools. This file exercises **every pool
kind the SDK offers** and every knob on their encap blocks — both allocation
modes on the pools, all three block allocation modes (static / dynamic /
inherit) and both block roles (external / internal). The values are illustrative,
chosen to cover the SDK surface, not to model a real VLAN plan.

This file owns only its ``niwaki-it-*`` pools; ``wipe(aci)`` removes them and is
run by hand (never by the suite).
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import infra
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

# One pool per (kind, allocation mode) so the DN is deterministic for wipe().
VLAN_STATIC = "niwaki-it-vlan-static"
VLAN_DYNAMIC = "niwaki-it-vlan-dynamic"
VXLAN = "niwaki-it-vxlan"
VSAN_STATIC = "niwaki-it-vsan-static"
MCAST = "niwaki-it-mcast"


def test_vlan_pools(live_aci: Niwaki) -> None:
    fab = infra()

    # Static pool: statically-allocated and inherited blocks, both roles.
    static = fab.vlan_pool(
        VLAN_STATIC,
        "static",
        description="Static VLAN pool - manually assigned encapsulations.",
    )
    static.range(
        "vlan-100",
        "vlan-199",
        allocation_mode="static",
        role="external",
        description="Statically-allocated external VLANs (border/L2Out facing).",
    )
    static.range(
        "vlan-200",
        "vlan-299",
        allocation_mode="inherit",
        role="internal",
        description="Inherited-mode internal VLANs.",
    )

    # Dynamic pool: dynamically-allocated and inherited blocks, both roles.
    dynamic = fab.vlan_pool(
        VLAN_DYNAMIC,
        "dynamic",
        description="Dynamic VLAN pool - APIC-allocated encapsulations (VMM).",
    )
    dynamic.range(
        "vlan-1000",
        "vlan-1099",
        allocation_mode="dynamic",
        role="external",
        description="Dynamically-allocated external VLANs.",
    )
    dynamic.range(
        "vlan-1100",
        "vlan-1199",
        allocation_mode="inherit",
        role="internal",
        description="Inherited-mode internal VLANs in the dynamic pool.",
    )

    fab.push(live_aci)


def test_vxlan_pool(live_aci: Niwaki) -> None:
    fab = infra()
    pool = fab.vxlan_pool(VXLAN, description="VXLAN encapsulation namespace.")
    pool.range(
        "vxlan-5000",
        "vxlan-5999",
        description="VXLAN encapsulation block.",
    )
    fab.push(live_aci)


def test_vsan_pools(live_aci: Niwaki) -> None:
    # VSAN pools are static-only — the APIC rejects a dynamic allocation mode on
    # VSAN, so this exercises static + inherited blocks across both roles.
    fab = infra()
    static = fab.vsan_pool(
        VSAN_STATIC,
        "static",
        description="Static VSAN pool - VSANs are statically allocated.",
    )
    static.range(
        "vsan-1",
        "vsan-100",
        allocation_mode="static",
        role="external",
        description="Statically-allocated external VSANs.",
    )
    static.range(
        "vsan-200",
        "vsan-299",
        allocation_mode="inherit",
        role="internal",
        description="Inherited-mode internal VSANs.",
    )

    fab.push(live_aci)


def test_mcast_address_pool(live_aci: Niwaki) -> None:
    fab = infra()
    pool = fab.mcast_addr_pool(
        MCAST,
        description="Multicast-address pool for VXLAN BUM traffic.",
    )
    pool.range(
        "224.0.0.1",
        "224.0.0.100",
        description="Multicast group-address block.",
    )
    fab.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (
        f"uni/infra/vlanns-[{VLAN_STATIC}]-static",
        f"uni/infra/vlanns-[{VLAN_DYNAMIC}]-dynamic",
        f"uni/infra/vxlanns-{VXLAN}",
        f"uni/infra/vsanns-[{VSAN_STATIC}]-static",
        f"uni/infra/maddrns-{MCAST}",
    ):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
