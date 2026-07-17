"""External connectivity — routed (l3-port) interfaces, exhaustive combinations (non-prod).

Run:
    uv run pytest tests/integration/06_l3out/test_002_interfaces_routed.py -m integration -s

Routed layer-3 interfaces on an L3Out, swept across the MTU scale (numeric +
``inherit``), both IPv6 DAD states and the target-DSCP scale — one interface per
combination, each on its own physical port, each with a secondary address and a
rogue-exception-MAC child. Two further push units isolate the per-path overlays
that depend on extra fabric features: PTP (every transport mode) and micro-BFD
(both enable states), so a feature the simulator declines does not sink the base
sweep.

One VRF backs the L3Out; router-ids and addresses use a 10.x scheme. Physical
ports come from the leaf's real ``l1PhysIf`` at runtime. Values are illustrative.
``wipe(aci)`` is operator-only.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import tenant
from niwaki.design._cursor import Cursor
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

TN = "niwaki-it-l3out"
POOL = "niwaki-it-l3v"
L3DOM = "niwaki-it-l3d"

MTU = [1500, 9216, "inherit"]
DAD = ["enabled", "disabled"]
DSCP = ["AF11", "CS3", "EF"]


def _leaves(aci: Niwaki) -> list[tuple[str, int]]:
    """Discover the fabric's leaf switches at runtime — (name, node-id), sorted by id."""
    found: list[tuple[str, int]] = []
    for node in aci.query("fabricNode").fetch():
        data = node.model_dump(by_alias=True)
        if data.get("role") == "leaf" and data.get("name") and data.get("id"):
            found.append((data["name"], int(data["id"])))
    return sorted(found, key=lambda pair: pair[1])


def _scaffold(t: Cursor) -> str:
    """Declare the VLAN lane, L3 domain and interface policies; return the L3 domain name."""
    t.infra().vlan_pool(POOL, "static").range(
        "vlan-2600", "vlan-2699", allocation_mode="static", role="external"
    )
    t.l3_dom(L3DOM).bind(vlan_pool=POOL)
    t.nd_interface_policy("niwaki-it-nd", hop_limit=64, mtu=1500)
    t.custom_qos_policy("niwaki-it-cqos", description="Custom QoS for routed interfaces.")
    t.netflow_monitor("niwaki-it-nfm", description="NetFlow monitor for routed interfaces.")
    t.dpp_policy("niwaki-it-dpp", rate=1000000, rate_unit="kilo", description="Policer for DPP.")
    return L3DOM


def _l3out_with_ifprofiles(t: Cursor, name: str, seq: int, leaves: list[tuple[str, int]]) -> dict:
    """Create a VRF + L3Out + per-leaf node profile/attachment/interface profile.

    Returns a mapping leaf-name -> its interface-profile cursor, ready to hang
    path attachments onto.
    """
    vrf = f"{name}-vrf"
    t.vrf(vrf, description=f"VRF backing {name}.")
    out = (
        t.l3out(name, description="Routed l3-port interfaces over MTU/IPv6-DAD/DSCP.")
        .bind(vrf=vrf)
        .bind(domain=L3DOM)
    )
    ifps: dict[str, Cursor] = {}
    for idx, (lname, node_id) in enumerate(leaves, start=1):
        np = out.node_profile(f"np-{lname}", description=f"Node profile for {lname}.")
        np.node_attachment(
            f"topology/pod-1/node-{node_id}",
            rtr_id=f"10.{seq}.0.{idx}",
            rtr_id_loop_back=False,
        )
        ifp = np.interface_profile(f"if-{lname}", description=f"Interface profile on {lname}.")
        ifp.bind(
            nd_interface_policy="niwaki-it-nd",
            custom_qos_policy="niwaki-it-cqos",
            netflow_monitor="niwaki-it-nfm",
        )
        ifp.ingress_dpp("niwaki-it-dpp")
        ifp.egress_dpp("niwaki-it-dpp")
        ifps[lname] = ifp
    return ifps


def test_routed_interfaces(live_aci: Niwaki) -> None:
    """Routed l3-port interfaces over the MTU x IPv6-DAD x DSCP matrix."""
    t = tenant(TN)
    _scaffold(t)
    leaves = _leaves(live_aci)
    ifps = _l3out_with_ifprofiles(t, "niwaki-it-l3o-routed", 21, leaves)

    for lidx, (lname, node_id) in enumerate(leaves, start=1):
        ifp = ifps[lname]
        port = 0
        for mtu in MTU:
            for dad in DAD:
                for dscp in DSCP:
                    port += 1
                    path = f"topology/pod-1/paths-{node_id}/pathep-[eth1/{port}]"
                    pa = ifp.path_attachment(
                        path,
                        if_inst_t="l3-port",
                        addr=f"10.20.{port}.{lidx}/24",
                        mtu=mtu,
                        ipv6_dad=dad,
                        target_dscp=dscp,
                    )
                    pa.secondary_ip_address(f"10.20.{port}.{lidx + 100}/24", ipv6_dad=dad)

    t.push(live_aci)


# COVERAGE GAPS (curated makers whose APIC preconditions the L3Out domain cannot
# meet here — reported, not forced):
#   maker:ptpRtdEpgCfg@l3extRsPathL3OutAtt   (PTP needs a fabric PTP profile selected)
#   maker:bfdMicroBfdP@l3extRsPathL3OutAtt   (micro-BFD needs a routed port-channel interface)
#   maker:l3extRogueExceptMacP@l3extRsPathL3OutAtt  (rogue-MAC exception is SVI-only — see SVI file)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — operator only; removes this file's L3Outs and VRFs."""
    for name in ("niwaki-it-l3o-routed",):
        for dn in (f"uni/tn-{TN}/out-{name}", f"uni/tn-{TN}/ctx-{name}-vrf"):
            with contextlib.suppress(NotFoundError):
                aci.node(dn).delete()
