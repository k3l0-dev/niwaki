"""L4-L7 services — device managers, chassis, firewall parameters (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/07_services/test_007_device_support.py -m integration -s

The operator declares the device-support objects: L4-L7 device managers and chassis
(each with credentials and a management interface across a spread of server ports), and
firewall-parameter sets (``vnsFWReq``) sweeping every named protocol value and a range
of destination ports.

Combination sweep — not a production catalogue. ``wipe(aci)`` (operator-only) drops the
dedicated tenant.

The ``consumer`` / ``provider`` connector tokens on ``vnsFWReq`` are left at their
defaults — the APIC only accepts device-package connector names there.

Universal children (``tagInst`` / ``tagExtMngdInst``) carry no typed maker in the DSL.

# COVERAGE GAPS (curated children with no reachable maker/bind — reported, not forced):
#   - bind:vnsRsDevMgrToMDevMgr / vnsRsDevMgrEpg / vnsRsDevEpg @vnsDevMgr and
#     bind:vnsRsChassisToMChassis / vnsRsChassisEpg / vnsRsDevEpg @vnsChassis
#   - maker:vnsCCredSecret @vnsDevMgr|vnsChassis (secret credentials)
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import tenant
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

TN = "niwaki-it-devsup"

# Every named protocol value on vnsFWReq, plus a destination port each.
PROTOCOLS = (
    ("tcp", 443),
    ("udp", 53),
    ("icmp", 0),
    ("icmpv6", 0),
    ("igmp", 0),
    ("ospfigp", 0),
    ("eigrp", 0),
    ("pim", 0),
    ("l2tp", 1701),
    ("egp", 0),
    ("igp", 0),
    ("unspecified", 0),
)

SERVER_PORTS = (443, 8443, 22, 830)


def test_device_managers(live_aci: Niwaki) -> None:
    tn = tenant(TN, description="Device manager, chassis and firewall-parameter sweep.")

    for index, port in enumerate(SERVER_PORTS):
        manager = tn.device_manager(
            f"niwaki-it-dm{index}", description=f"Device manager on port {port}."
        )
        manager.credentials(name="admin", value="illustrative-secret")
        manager.management_interface(f"10.30.0.{index + 10}", name="mgmt", port=port)

    tn.push(live_aci)


def test_chassis(live_aci: Niwaki) -> None:
    tn = tenant(TN, description="Device manager, chassis and firewall-parameter sweep.")

    for index, port in enumerate(SERVER_PORTS):
        chassis = tn.chassis(f"niwaki-it-chassis{index}", description=f"Chassis on port {port}.")
        chassis.credentials(name="admin", value="illustrative-secret")
        chassis.management_interface(f"10.31.0.{index + 10}", name="mgmt", port=port)

    tn.push(live_aci)


def test_firewall_parameters(live_aci: Niwaki) -> None:
    tn = tenant(TN, description="Device manager, chassis and firewall-parameter sweep.")

    for index, (protocol, dport) in enumerate(PROTOCOLS):
        tn.firewall_parameters(
            "niwaki-it-ct",
            "niwaki-it-graph",
            "FW1",
            f"acl-{protocol}",
            description=f"Firewall ACE for {protocol}.",
            ace=f"permit-{protocol}",
            destination_from_port=dport,
            destination_to_port=dport,
            acl_destination_type=0,
            host_network_ip_address=f"10.32.{index}.0",
            external_interface_name="outside-if",
            internal_interface_name="inside-if",
            nw_obj_name="web-servers",
            protocol=protocol,
        )

    tn.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (f"uni/tn-{TN}",):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
