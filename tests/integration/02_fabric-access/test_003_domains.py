"""Fabric access — domains and the attachable entity profile (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/02_fabric-access/test_003_domains.py -m integration -s

With the encapsulation pools in place, the operator declares the **domains** that
bind those encapsulations to physical resources — a physical, an L3-external, an
L2-external and a Fibre-Channel domain — then the **attachable access entity
profile** (AAEP) that ties the domains onto the interfaces. This exercises every
domain kind the SDK offers and their pool references, plus an AAEP attached to
several domains at once.

Everything lives in one closed-world design so the ``bind(...)`` references
resolve in-design (the domains draw from pools this file declares). Values are
illustrative. ``wipe(aci)`` (operator-only) removes the whole set.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import infra
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

DOM_VLAN = "niwaki-it-dom-vlan"
DOM_VSAN = "niwaki-it-dom-vsan"
PHYS = "niwaki-it-phys"
L3 = "niwaki-it-l3"
L2 = "niwaki-it-l2"
FC = "niwaki-it-fc"
AAEP = "niwaki-it-aaep"


def test_domains_and_aaep(live_aci: Niwaki) -> None:
    fab = infra()

    # Pools the domains draw from — declared in-design so bind() resolves them.
    fab.vlan_pool(DOM_VLAN, "static", description="VLAN pool for the access domains.").range(
        "vlan-300", "vlan-399", allocation_mode="static", role="external"
    )
    fab.vsan_pool(DOM_VSAN, "static", description="VSAN pool for the FC domain.").range(
        "vsan-300", "vsan-399", allocation_mode="static", role="external"
    )

    # One domain per kind, each bound to its encapsulation pool.
    # (Domain classes carry no description field on the APIC.)
    fab.phys_dom(PHYS).bind(vlan_pool=DOM_VLAN)
    fab.l3_dom(L3).bind(vlan_pool=DOM_VLAN)
    fab.l2_dom(L2).bind(vlan_pool=DOM_VLAN)
    fab.fc_dom(FC).bind(vlan_pool=DOM_VLAN).bind(vsan_pool=DOM_VSAN)

    # The AAEP ties several domains onto the interfaces that will use it.
    fab.aaep(AAEP, description="AAEP attaching the physical, L3 and L2 domains.").bind(
        domain=PHYS
    ).bind(domain=L3).bind(domain=L2)

    fab.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (
        f"uni/infra/attentp-{AAEP}",
        f"uni/phys-{PHYS}",
        f"uni/l3dom-{L3}",
        f"uni/l2dom-{L2}",
        f"uni/fc-{FC}",
        f"uni/infra/vlanns-[{DOM_VLAN}]-static",
        f"uni/infra/vsanns-[{DOM_VSAN}]-static",
    ):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
