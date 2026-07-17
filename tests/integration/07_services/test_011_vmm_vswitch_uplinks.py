"""VMM — vSwitch policies and uplinks, combination coverage (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/07_services/test_011_vmm_vswitch_uplinks.py -m integration -s

Under a VMware VMM domain the operator declares the vSwitch policy group and sweeps the
enhanced-LACP policies across every load-balancing mode, rotating the LACP mode and the
number of links. A second test declares the uplink container and a spread of uplink
policies.

Combination sweep — not a production catalogue. ``wipe(aci)`` (operator-only) deletes
the named domains.

Universal children (``tagInst`` / ``tagExtMngdInst``) carry no typed maker in the DSL.
"""

from __future__ import annotations

import contextlib
import itertools

import pytest

from niwaki import Niwaki
from niwaki.design import design
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

VENDOR = "VMware"
VSWITCH_DOM = "niwaki-it-vsw-dom"
UPLINK_DOM = "niwaki-it-upl-dom"

LOADBALANCING_MODES = (
    "dst-ip",
    "dst-ip-l4port",
    "dst-ip-l4port-vlan",
    "dst-ip-vlan",
    "dst-l4port",
    "dst-mac",
    "src-dst-ip",
    "src-dst-ip-l4port",
    "src-dst-ip-l4port-vlan",
    "src-dst-ip-vlan",
    "src-dst-l4port",
    "src-dst-mac",
    "src-ip",
    "src-ip-l4port",
    "src-ip-l4port-vlan",
    "src-ip-vlan",
    "src-l4port",
    "src-mac",
    "src-port-id",
    "vlan",
)


def test_vmm_vswitch(live_aci: Niwaki) -> None:
    dsn = design()
    provider = dsn.vmm_provider(VENDOR)
    lacp_cycle = itertools.cycle(("active", "passive"))

    # Each enhanced-LAG policy reserves DVS uplinks, and a domain has a finite pool, so
    # the load-balancing-mode sweep is spread four modes per domain across five domains.
    chunk = 4
    for dom_index in range(0, len(LOADBALANCING_MODES), chunk):
        dom = provider.vmm_dom(f"{VSWITCH_DOM}-{dom_index // chunk}", encap_mode="vlan")
        vswitch = dom.vswitch_policy_group(description="Distributed vSwitch policies.")
        for offset, mode in enumerate(LOADBALANCING_MODES[dom_index : dom_index + chunk]):
            vswitch.enhanced_lacp_policy(
                f"elag-{dom_index + offset:02d}",
                loadbalancing_mode=mode,
                lacp_mode=next(lacp_cycle),
                number_of_links=2,
                id=offset + 1,
            )

    dsn.push(live_aci)


def test_vmm_uplinks(live_aci: Niwaki) -> None:
    dsn = design()
    dom = dsn.vmm_provider(VENDOR).vmm_dom(UPLINK_DOM, encap_mode="vlan")
    uplinks = dom.uplink_policy_container(number_of_uplinks="8")
    for uplink_id in range(1, 9):
        uplinks.uplink_policy(uplink_id, uplink_name=f"uplink-{uplink_id}", id=uplink_id)

    dsn.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    vswitch_doms = [f"{VSWITCH_DOM}-{i}" for i in range((len(LOADBALANCING_MODES) + 3) // 4)]
    for dom in (*vswitch_doms, UPLINK_DOM):
        with contextlib.suppress(NotFoundError):
            aci.node(f"uni/vmmp-{VENDOR}/dom-{dom}").delete()
