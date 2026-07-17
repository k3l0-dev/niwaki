"""VMM — controllers and host availability, combination coverage (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/07_services/test_010_vmm_controllers.py -m integration -s

Under VMware VMM domains the operator declares vCenter controllers sweeping every DVS
version, rotating the stats-collection, N1KV-stats and VXLAN-deployment-preference
enums, plus a cluster controller. A second test declares the host-availability policy
with a desired host state per status value and a protected VM group.

Environment note: the controllers land ACI-side config but need a reachable vCenter to
sync inventory and for the protected VM group to resolve — they fault until then.

Combination sweep — not a production catalogue. ``wipe(aci)`` (operator-only) deletes
the named domains.

Universal children (``tagInst`` / ``tagExtMngdInst``) carry no typed maker in the DSL.

# COVERAGE GAPS (curated children with no reachable maker/bind — reported, not forced):
#   - maker:vmmAgtStatus@vmmCtrlrP / vmmPlInf@vmmCtrlrP
#   - bind:vmmRsAcc / vmmRsMgmtEPg / vmmRsCtrlrPMonPol / vmmRsMcastAddrNs /
#     vmmRsToExtDevMgr / vmmRsVmmCtrlrP / vmmRsVxlanNs @vmmCtrlrP
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
CTRL_DOM = "niwaki-it-ctrl-dom"
HA_DOM = "niwaki-it-ha-dom"

DVS_VERSIONS = ("5.1", "5.5", "6.0", "6.5", "6.6", "7.0", "8.0", "not-applicable", "unmanaged")
STATS_MODES = ("disabled", "enabled", "unknown")
VXLAN_PREFS = ("nsx", "vxlan")
HOST_STATES = ("gray", "green", "red", "yellow")


def test_vmm_controllers(live_aci: Niwaki) -> None:
    dsn = design()
    dom = dsn.vmm_provider(VENDOR).vmm_dom(CTRL_DOM, encap_mode="vlan")

    stats_cycle = itertools.cycle(STATS_MODES)
    n1kv_cycle = itertools.cycle(STATS_MODES)
    vxlan_cycle = itertools.cycle(VXLAN_PREFS)

    for index, dvs_version in enumerate(DVS_VERSIONS):
        controller = dom.vmm_controller(
            f"vcenter-{index}",
            hostname_or_ip_address=f"10.50.0.{index + 10}",
            dvs_version=dvs_version,
            mode="default",
            type="vm",
            stats_collection=next(stats_cycle),
            n1kv_stats_mode=next(n1kv_cycle),
            vxlan_deployment_preference=next(vxlan_cycle),
            port=443,
            datacenter="Lab-DC",
            seq_num=index + 1,
        )
        if index == 0:
            controller.cluster_controller(
                "vcenter-node-1",
                hostname_or_ip_address="10.50.0.100",
                datacenter="Lab-DC",
                port=443,
            )

    dsn.push(live_aci)


def test_vmm_host_availability(live_aci: Niwaki) -> None:
    dsn = design()
    dom = dsn.vmm_provider(VENDOR).vmm_dom(HA_DOM, encap_mode="vlan")
    controller = dom.vmm_controller(
        "vcenter-ha",
        hostname_or_ip_address="10.51.0.10",
        mode="default",
        type="vm",
        port=443,
        datacenter="Lab-DC",
    )
    availability = controller.host_availability_policy(name="host-availability")
    for index, state in enumerate(HOST_STATES):
        availability.host_desired_state(f"esxi-{index}", desired_state_for_the_host=state)
    availability.protect_vm_group(f"uni/vmmp-{VENDOR}/dom-{HA_DOM}/vmgrp-protected")

    dsn.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dom in (CTRL_DOM, HA_DOM):
        with contextlib.suppress(NotFoundError):
            aci.node(f"uni/vmmp-{VENDOR}/dom-{dom}").delete()
