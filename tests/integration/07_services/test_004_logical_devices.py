"""L4-L7 services — logical device clusters, combination coverage (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/07_services/test_004_logical_devices.py -m integration -s

The operator declares a spread of L4-L7 device clusters (``vnsLDevVip``) covering both
declarable device types (physical, virtual), every function type and service type, both
tenancies, and the managed / promiscuous / trunking / copy / active-active booleans.
Each cluster carries a concrete device (concrete interfaces + device params), cluster
credentials, logical interfaces, and a management interface — the management interfaces
sweep every IP-allocation type and both in-band states.

Rules learned live and honoured here: ``encap`` on a concrete interface is only valid
on an active-active cluster; the enhanced-LAG policy name is only valid on a virtual
cluster. The cloud device type is out of scope (needs a cloud APIC).

Combination sweep — not a production catalogue. ``wipe(aci)`` (operator-only) drops the
dedicated tenant, which cascades every device.

Universal children (``tagInst`` / ``tagExtMngdInst``) carry no typed maker in the DSL.

# COVERAGE GAPS (curated children with no reachable maker/bind — reported, not forced):
#   - maker:vnsDevParam@vnsLDevVip / vnsMgmtLIf@vnsLDevVip / vnsCCredSecret@vnsLDevVip
#   - bind:vnsRsALDevToPhysDomP / vnsRsALDevToDomP / vnsRsALDevToVxlanInstP /
#     vnsRsALDevToDevMgr / vnsRsMDevAtt / vnsRsDevEpg / vnsRsLDevVipToInstPol @vnsLDevVip
#   - maker:vnsCCred / vnsCMgmt / vnsCCredSecret / vnsHAPortGroup @vnsCDev and
#     bind:vnsRsCDevToChassis / vnsRsCDevToCtrlrP / vnsRsCDevTemplateToAddrInst @vnsCDev
#   - concrete-interface path attachment (vnsRsCIfPathAtt) is fabric-discovery-dependent
#   - bind:vnsRsLIfDomP@vnsLIf (logical-interface domain) has no curated bind — this
#     blocks active-active clusters (they require it), so active_active_mode stays False
"""

from __future__ import annotations

import contextlib
import itertools

import pytest

from niwaki import Niwaki
from niwaki.design import tenant
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

TN = "niwaki-it-ldev"

# (device_type, function_type, svc_type, tenancy, managed, promiscuous, trunking,
#  is_copy, active_active)
DEVICE_SPECS = (
    # ── Managed physical devices (function type + service type spread) ────────────
    ("PHYSICAL", "GoTo", "FW", "single-Context", True, False, False, False, False),
    ("PHYSICAL", "GoThrough", "ADC", "multi-Context", True, False, False, False, False),
    ("PHYSICAL", "None", "NATIVELB", "single-Context", True, False, False, False, False),
    ("PHYSICAL", "GoTo", "OTHERS", "multi-Context", True, False, False, False, False),
    # ── Managed virtual devices (promiscuous x trunking factored) ─────────────────
    ("VIRTUAL", "GoTo", "FW", "single-Context", True, False, False, False, False),
    ("VIRTUAL", "GoThrough", "ADC", "multi-Context", True, True, True, False, False),
    ("VIRTUAL", "None", "NATIVELB", "single-Context", True, True, False, False, False),
    ("VIRTUAL", "GoTo", "ADC", "multi-Context", True, False, True, False, False),
    # ── Unmanaged L1 / L2 devices (must be physical and unmanaged) ────────────────
    ("PHYSICAL", "L1", "OTHERS", "single-Context", False, False, False, False, False),
    ("PHYSICAL", "L2", "OTHERS", "multi-Context", False, False, False, False, False),
    # ── Copy device (physical, unmanaged, function type None) ─────────────────────
    ("PHYSICAL", "None", "COPY", "single-Context", False, False, False, True, False),
)
# active_active_mode=True (and, with it, encap on the concrete interface) is not
# exercised: the APIC requires a logical-interface domain (RsLIfDomP -> physDomP) on an
# active-active L1/L2 device, and vnsRsLIfDomP@vnsLIf has no curated bind (see the
# COVERAGE GAPS block above). The active_active handling below stays generic.

IP_ALLOCATION_TYPES = ("default", "dhcp", "fixed")


def test_logical_devices(live_aci: Niwaki) -> None:
    tn = tenant(TN, description="Logical device type and service-type matrix.")

    alloc_cycle = itertools.cycle(IP_ALLOCATION_TYPES)
    in_band_cycle = itertools.cycle((True, False))

    for index, spec in enumerate(DEVICE_SPECS):
        (
            device_type,
            function_type,
            svc_type,
            tenancy,
            managed,
            promiscuous,
            trunking,
            is_copy,
            active_active,
        ) = spec
        is_virtual = device_type == "VIRTUAL"

        ldev = tn.logical_device(
            f"niwaki-it-ld{index:02d}",
            active_active_mode=active_active,
            tenancy=tenancy,
            device_type=device_type,
            function_type=function_type,
            managed=managed,
            mode="legacy-Mode",
            promiscuous_mode=promiscuous,
            svc_type=svc_type,
            trunking=trunking,
            is_copy=is_copy,
        )

        concrete = ldev.concrete_device(
            "cdev-1",
            context_label="prod",
            management_address=f"10.10.{index}.10",
            vcenter_name="vcenter-lab" if is_virtual else None,
            vm_name=f"svc-vm-{index}" if is_virtual else None,
        )
        # encap on a concrete interface is only accepted on an active-active cluster.
        concrete.concrete_interface(
            "if-0",
            encap="vlan-900" if active_active else None,
            vnic="Network adapter 2" if is_virtual else None,
        )
        concrete.concrete_interface(
            "if-1",
            encap="vlan-901" if active_active else None,
            vnic="Network adapter 3" if is_virtual else None,
        )
        concrete.device_param("boot", key="bootMode", value="uefi")

        # Encap placement is exclusive: on an active-active cluster it lives on the
        # concrete interface (above); otherwise on the logical interface here.
        # enhanced_lag_policy_name is omitted: it references an enhanced-LAG policy that
        # must live in a VMM domain associated to the device (vnsRsALDevToDomP — a
        # coverage gap, no bind), so the APIC rejects the name without that association.
        lif_encap_consumer = None if active_active else "vlan-910"
        lif_encap_provider = None if active_active else "vlan-911"
        ldev.logical_interface("consumer", encap=lif_encap_consumer)
        ldev.logical_interface("provider", encap=lif_encap_provider)

        # Credentials and a management interface only apply to managed devices.
        if managed:
            ldev.credentials(name="admin", value="illustrative-secret")
            alloc = next(alloc_cycle)
            if alloc == "fixed":
                ldev.management_interface(
                    name="mgmt",
                    ip_address=f"10.20.{index}.11",
                    ip_allocation_type="fixed",
                    is_in_band=next(in_band_cycle),
                    gateway_ip_address=f"10.20.{index}.1",
                    subnet_mask="255.255.255.0",
                    dns_domain="lab.example.com",
                    port=443,
                    port_group_name="mgmt-pg",
                    vnic_name="Network adapter 1" if is_virtual else None,
                )
            else:
                ldev.management_interface(
                    name="mgmt",
                    ip_allocation_type=alloc,
                    is_in_band=next(in_band_cycle),
                    port=443,
                )

    tn.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (f"uni/tn-{TN}",):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
