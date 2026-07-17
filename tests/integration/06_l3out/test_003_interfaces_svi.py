"""External connectivity — SVI, sub-interface and floating-SVI interfaces (non-prod).

Run:
    uv run pytest tests/integration/06_l3out/test_003_interfaces_svi.py -m integration -s

SVIs are swept across the tag-mode x encap-scope x autostate matrix (one SVI per
combination, each on its own port and VLAN from the 2600-2699 lane), each with a
secondary address and a rogue-exception-MAC child (SVI-only). Sub-interfaces are
swept across the tag modes. Floating SVIs cover the anchor-node model with per-side
member nodes, secondary addresses, an ND prefix profile and a bridge-domain
profile container.

One VRF backs the L3Out; encaps come from the shared VLAN lane, addresses use a
10.x scheme. Values are illustrative. ``wipe(aci)`` is operator-only.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import ref, tenant
from niwaki.design._cursor import Cursor
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

TN = "niwaki-it-l3out"
POOL = "niwaki-it-l3v"
L3DOM = "niwaki-it-l3d"
L3OUT = "niwaki-it-l3o-svi"
VRF = "niwaki-it-l3o-svi-vrf"

MODES = ["regular", "native", "untagged"]
SCOPES = ["local", "ctx"]
AUTOSTATE = ["enabled", "disabled"]


def _leaves(aci: Niwaki) -> list[tuple[str, int]]:
    """Discover the fabric's leaf switches at runtime — (name, node-id), sorted by id."""
    found: list[tuple[str, int]] = []
    for node in aci.query("fabricNode").fetch():
        data = node.model_dump(by_alias=True)
        if data.get("role") == "leaf" and data.get("name") and data.get("id"):
            found.append((data["name"], int(data["id"])))
    return sorted(found, key=lambda pair: pair[1])


def _scaffold(t: Cursor) -> Cursor:
    """VLAN lane + L3 domain; returns the L3Out with a node profile / interface profile per leaf."""
    t.infra().vlan_pool(POOL, "static").range(
        "vlan-2600", "vlan-2699", allocation_mode="static", role="external"
    )
    t.l3_dom(L3DOM).bind(vlan_pool=POOL)
    t.vrf(VRF, description="VRF for the SVI L3Out.")
    return t.l3out(L3OUT, description="SVI / floating-SVI L3Out.").bind(vrf=VRF).bind(domain=L3DOM)


def test_svi_interfaces(live_aci: Niwaki) -> None:
    """SVIs over the tag-mode x encap-scope x autostate matrix, each with rogue-MAC."""
    t = tenant(TN)
    out = _scaffold(t)
    leaves = _leaves(live_aci)

    for lidx, (lname, node_id) in enumerate(leaves, start=1):
        np = out.node_profile(f"np-{lname}", description=f"Node profile for {lname}.")
        np.node_attachment(
            f"topology/pod-1/node-{node_id}", rtr_id=f"10.3.0.{lidx}", rtr_id_loop_back=False
        )
        ifp = np.interface_profile(f"if-{lname}", description=f"SVI interface profile on {lname}.")

        port = 40
        vlan = 2600
        for mode in MODES:
            for scope in SCOPES:
                for autostate in AUTOSTATE:
                    pa = ifp.path_attachment(
                        f"topology/pod-1/paths-{node_id}/pathep-[eth1/{port}]",
                        if_inst_t="ext-svi",
                        addr=f"10.30.{port}.{lidx}/24",
                        encap=f"vlan-{vlan}",
                        mode=mode,
                        encap_scope=scope,
                        autostate=autostate,
                        mtu="1500",
                    )
                    pa.secondary_ip_address(f"10.30.{port}.{lidx + 100}/24")
                    pa.rogue_exception_mac(enable_all_macs=(port % 2 == 0))
                    port += 1
                    vlan += 1

    t.push(live_aci)


def test_subinterfaces(live_aci: Niwaki) -> None:
    """Sub-interfaces (dot1q) over the tag modes."""
    t = tenant(TN)
    out = _scaffold(t)
    leaves = _leaves(live_aci)

    for lidx, (lname, node_id) in enumerate(leaves, start=1):
        np = out.node_profile(f"np-{lname}")
        np.node_attachment(
            f"topology/pod-1/node-{node_id}", rtr_id=f"10.3.0.{lidx}", rtr_id_loop_back=False
        )
        ifp = np.interface_profile(f"if-{lname}")
        # Sub-interfaces are always tagged (mode regular); sweep encap-scope + MTU.
        for i, (scope, mtu) in enumerate([("local", "1500"), ("ctx", "9000")]):
            port = 60 + i
            pa = ifp.path_attachment(
                f"topology/pod-1/paths-{node_id}/pathep-[eth1/{port}]",
                if_inst_t="sub-interface",
                addr=f"10.31.{port}.{lidx}/24",
                encap=f"vlan-{2640 + i}",
                mode="regular",
                encap_scope=scope,
                mtu=mtu,
            )
            pa.secondary_ip_address(f"10.31.{port}.{lidx + 100}/24")

    t.push(live_aci)


def test_floating_svi(live_aci: Niwaki) -> None:
    """Floating SVIs: anchor node, per-side member nodes, secondary, ND prefix, BD profile."""
    t = tenant(TN)
    out = _scaffold(t)
    leaves = _leaves(live_aci)
    t.nd_ra_prefix_policy("niwaki-it-ndpfx", valid_lifetime=2592000, preferred_lifetime=604800)

    for lidx, (lname, node_id) in enumerate(leaves, start=1):
        np = out.node_profile(f"np-{lname}")
        np.node_attachment(
            f"topology/pod-1/node-{node_id}", rtr_id=f"10.3.0.{lidx}", rtr_id_loop_back=False
        )
        ifp = np.interface_profile(f"if-{lname}")
        for i, scope in enumerate(SCOPES):
            # The floating address lives on the dynamic-path attachment (the domain
            # bind), and must sit in the same subnet as the primary/anchor address —
            # +40 keeps it in 10.32.{i}.0/24 yet distinct from members A/B and the
            # secondary. Leaving it unset defaults it to 0.0.0.0 and the APIC raises a
            # subnet-mismatch config fault (F3744).
            fsvi = ifp.floating_svi(
                f"topology/pod-1/node-{node_id}",
                f"vlan-{2680 + i}",
                external_l3_interface_ip_address=f"10.32.{i}.{lidx}/24",
                external_interface_type="ext-svi",
                encap_scope=scope,
                encap_mode="regular",
                svi_autostate="enabled",
                mtu_size="1500",
                description=f"Floating SVI scope {scope}.",
            ).bind(domain=ref(L3DOM, floating_addr=f"10.32.{i}.{lidx + 40}/24"))
            fsvi.member_node_configuration("A", addr=f"10.32.{i}.{lidx + 10}/24")
            fsvi.member_node_configuration("B", addr=f"10.32.{i}.{lidx + 20}/24")
            fsvi.secondary_ip_address(f"10.32.{i}.{lidx + 30}/24")
            fsvi.nd_prefix_profile().bind(nd_ra_prefix_policy="niwaki-it-ndpfx")
            # COVERAGE GAP: bd_profile_container (l3extBdProfileCont) is accepted on a
            # floating SVI only when a *physical* domain is bound, not the L3 domain
            # used here, so it is not exercised on this L3-domain floating SVI.

    t.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — operator only; removes this file's L3Out and VRF."""
    for dn in (f"uni/tn-{TN}/out-{L3OUT}", f"uni/tn-{TN}/ctx-{VRF}"):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
