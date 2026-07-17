"""VMM — domain and vSwitch policy bindings (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/07_services/test_013_vmm_bindings.py -m integration -s

The operator wires the VMM domain's interface-policy overrides. In one closed-world
design it declares a VLAN pool, a VIP address pool, and CDP / LLDP / LACP / STP / MCP /
NetFlow / MTU policies, then binds them onto the VMware VMM domain and its vSwitch
policy group.

Rules learned live and honoured here: the multicast-address-namespace bind is not
supported on a VMM domain (omitted); the NetFlow exporter needs a real collector L4
port (the "unspecified" default is rejected as "Invalid Server Port 0").

The "firewall" bind (nwsFwPol) has no declarable maker anywhere in the vocabulary and is
not exercised (see COVERAGE GAPS).

Combination sweep — not a production catalogue. ``wipe(aci)`` (operator-only) deletes
the named domain and the closed-world bind targets it created.

Universal children (``tagInst`` / ``tagExtMngdInst``) carry no typed maker in the DSL.

# COVERAGE GAPS (curated children with no reachable maker/bind — reported, not forced):
#   - bind:vmmRsDefaultFwPol@vmmDomP / vmmRsVswitchOverrideFwPol@vmmVSwitchPolicyCont
#     ("firewall" -> nwsFwPol has no maker in the vocabulary)
#   - bind:mcast_pool -> vmmRsDomMcastAddrNs@vmmDomP (rejected by the APIC:
#     "McastAddr multicast address namespace is not supported for vmm domain")
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import design
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

VENDOR = "VMware"
DOM = "niwaki-it-bind-dom"
TN = "niwaki-it-vmm-bind"

VLAN = "niwaki-it-vmm-vlan"
ADDR = "niwaki-it-vmm-addr"
CDP = "niwaki-it-vmm-cdp"
LLDP = "niwaki-it-vmm-lldp"
LACP = "niwaki-it-vmm-lacp"
STP = "niwaki-it-vmm-stp"
MCP = "niwaki-it-vmm-mcp"
NETFLOW = "niwaki-it-vmm-nf"
L2MTU = "niwaki-it-vmm-l2"


def test_vmm_domain_policy_bindings(live_aci: Niwaki) -> None:
    dsn = design()

    # Closed-world bind targets across infra / fabric / tenant domains.
    infra = dsn.infra()
    infra.vlan_pool(VLAN, "dynamic").range("vlan-2700", "vlan-2799", allocation_mode="dynamic")
    infra.cdp_policy(CDP, admin_state="enabled")
    infra.lldp_policy(LLDP, receive_state="enabled", transmit_state="enabled")
    infra.lacp_policy(LACP, mode="active")
    infra.stp_policy(STP)
    infra.mcp_policy(MCP, admin_state="enabled")
    infra.netflow_vmm_exporter(NETFLOW, remote_entity_ip="10.60.0.50", remote_entity_l4_port=2055)
    dsn.fabric().fabric_l2_mtu_policy(L2MTU, mtu_size_for_fabric_ports=9000)
    dsn.tenant(TN, description="VIP address pool for the VMM bind sweep.").ip_address_pool(
        ADDR, ip_address="10.61.0.1/24", address_type="vip_range"
    )

    dom = dsn.vmm_provider(VENDOR).vmm_dom(DOM, encap_mode="vlan")
    # Domain-level default interface-policy overrides + pools (no mcast — unsupported).
    dom.bind(
        vlan_pool=VLAN,
        address_pool=ADDR,
        cdp=CDP,
        lldp=LLDP,
        lacp=LACP,
        stp=STP,
        l2_mtu=L2MTU,
    )

    # vSwitch-level interface-policy overrides.
    vswitch = dom.vswitch_policy_group(description="vSwitch overrides.")
    vswitch.bind(
        cdp=CDP,
        lldp=LLDP,
        lacp=LACP,
        stp=STP,
        mcp=MCP,
        l2_mtu=L2MTU,
        netflow=NETFLOW,
    )

    dsn.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (
        f"uni/vmmp-{VENDOR}/dom-{DOM}",
        f"uni/tn-{TN}",
        f"uni/infra/vlanns-[{VLAN}]-dynamic",
        f"uni/infra/cdpIfP-{CDP}",
        f"uni/infra/lldpIfP-{LLDP}",
        f"uni/infra/lacplagp-{LACP}",
        f"uni/infra/ifPol-{STP}",
        f"uni/infra/mcpIfP-{MCP}",
        f"uni/infra/vmmexporterpol-{NETFLOW}",
        f"uni/fabric/l2pol-{L2MTU}",
    ):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
