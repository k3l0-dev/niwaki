"""Act 4 — L3Out: the wave-1 vocabulary, live against the APIC.

The complete routed-exit chain in operator vocabulary — L3 domain, L3Out,
border node with router ID, routed sub-interface, OSPF on the link, external
EPG with its contract — plus the BGP flavor, day-2 drift, and cleanup.

Every maker/bind exercised here entered the vocabulary with wave 1
(2026-07-12); this act is the wave's live gate.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import Cursor, tenant
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

TENANT = "niwaki-l3out"
L3_DOM = "niwaki-wan"
NODE_DN = "topology/pod-1/node-101"
PATH_DN = "topology/pod-1/paths-101/pathep-[eth1/33]"


def _wipe(aci: Niwaki) -> None:
    for dn in (f"uni/tn-{TENANT}", f"uni/l3dom-{L3_DOM}"):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()


def l3out_design() -> Cursor:
    """The recipe design (docs/cookbook/l3out-basic.md), verbatim spirit."""
    config = tenant(TENANT, description="niwaki walkthrough act 4")
    config.vrf("prod")
    config.l3_dom(L3_DOM)
    config.ospf_interface_policy("ospf-p2p", network_type="p2p")

    l3out = config.l3out("wan").bind(vrf="prod", domain=L3_DOM)
    l3out.ospf()

    nodes = l3out.node_profile("border-leaves")
    nodes.node_attachment(NODE_DN, rtr_id="10.0.0.101")

    interfaces = nodes.interface_profile("uplinks")
    interfaces.path_attachment(
        PATH_DN, if_inst_t="sub-interface", addr="192.0.2.2/30", encap="vlan-3900"
    )
    interfaces.ospf_interface().bind(ospf_interface_policy="ospf-p2p")

    config.filter("any-ip").entry("ip", ethernet_type="ip")
    config.contract("outbound").set(scope="vrf").subject("all").bind(filter="any-ip")

    external = l3out.external_epg("internet")
    external.subnet("0.0.0.0/0")
    external.consume("outbound")
    return config


class Test4L3Out:
    """Wave-1 live gate: plan → push → observe → day-2 → cleanup."""

    def test_01_plan_then_atomic_push(self, live_aci: Niwaki) -> None:
        _wipe(live_aci)
        design = l3out_design()

        plan = design.push(live_aci, mode="plan")
        assert plan.has_changes
        assert f"uni/tn-{TENANT}/out-wan" in plan.creates

        report = design.push(live_aci)
        assert report.request_count == 1

    def test_02_replan_is_converged(self, live_aci: Niwaki) -> None:
        assert l3out_design().push(live_aci, mode="plan").has_changes is False

    def test_03_the_fabric_confirms_the_chain(self, live_aci: Niwaki) -> None:
        out = live_aci.tenant(TENANT).query("l3extOut").fetch()
        assert [o.name for o in out] == ["wan"]

        atts = live_aci.tenant(TENANT).query("l3extRsNodeL3OutAtt").fetch()
        assert [a.rtr_id for a in atts] == ["10.0.0.101"]

        paths = live_aci.tenant(TENANT).query("l3extRsPathL3OutAtt").fetch()
        assert [p.addr for p in paths] == ["192.0.2.2/30"]

        subnets = live_aci.tenant(TENANT).query("l3extSubnet").fetch()
        assert [s.subnet for s in subnets] == ["0.0.0.0/0"]  # D2: wire "ip" → subnet

    def test_04_bgp_flavor(self, live_aci: Niwaki) -> None:
        config = tenant(TENANT)
        config.vrf("prod")  # closed world: the bind target must be declared
        bgp = config.l3out("wan-bgp").bind(vrf="prod")
        bgp.bgp()
        peer = bgp.node_profile("border-leaves").bgp_peer("192.0.2.1")
        peer.autonomous_system_profile(autonomous_system_number=65002)

        bgp.push(live_aci, mode="staged")

        peers = live_aci.tenant(TENANT).query("bgpPeerP").fetch()
        assert [p.peer_address for p in peers] == ["192.0.2.1"]
        asns = live_aci.tenant(TENANT).query("bgpAsP").fetch()
        assert [a.autonomous_system_number for a in asns] == [65002]  # typed int

    def test_05_day2_is_a_smaller_design(self, live_aci: Niwaki) -> None:
        patch = tenant(TENANT).ospf_interface_policy("ospf-p2p", cost_of_interface="100")

        plan = patch.push(live_aci, mode="plan")
        (dn,) = list(plan.updates)
        assert dn.endswith("ospfIfPol-ospf-p2p")

        patch.push(live_aci)
        assert patch.push(live_aci, mode="plan").has_changes is False

    # The tenant protocol-policy coverage lives in act 3 (tenant domain):
    # test_03_tenant.Test3ProtocolPolicies.
    #
    # No end-of-run cleanup: the L3Out stays on the simulator for manual
    # investigation.  test_01 wipes at the START, keeping re-runs
    # deterministic.
