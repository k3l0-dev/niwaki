"""External connectivity — routing-protocol x interface-type matrix (non-prod).

Run:
    uv run pytest tests/integration/06_l3out/test_012_protocol_interface_matrix.py -m integration -s

A routing protocol and an interface type cannot vary on one L3Out (OSPF and EIGRP
are mutually exclusive on an L3Out, and the protocol interface is a singleton per
interface profile), so this file factors the cross-product onto its own L3Out per
cell: each of OSPF and EIGRP over each of the routed (l3-port), sub-interface and
SVI interface types. Every combination lands as a valid, independent L3Out.

Each L3Out gets its own VRF; routed ports are dedicated, tagged interfaces draw
encaps from the shared lane. Values are illustrative. ``wipe(aci)`` is
operator-only.
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

PROTOCOLS = ["ospf", "eigrp"]
# interface type -> (routed port base, encap base). Routed ports are exclusive.
IFTYPES = {
    "routed": ("l3-port", 19, None),
    "sub": ("sub-interface", 25, 2685),
    "svi": ("ext-svi", 26, 2687),
}


def _leaves(aci: Niwaki) -> list[tuple[str, int]]:
    """Discover the fabric's leaf switches at runtime — (name, node-id), sorted by id."""
    found: list[tuple[str, int]] = []
    for node in aci.query("fabricNode").fetch():
        data = node.model_dump(by_alias=True)
        if data.get("role") == "leaf" and data.get("name") and data.get("id"):
            found.append((data["name"], int(data["id"])))
    return sorted(found, key=lambda pair: pair[1])


def _scaffold(t: Cursor) -> None:
    t.infra().vlan_pool(POOL, "static").range(
        "vlan-2600", "vlan-2699", allocation_mode="static", role="external"
    )
    t.l3_dom(L3DOM).bind(vlan_pool=POOL)
    t.ospf_interface_policy("niwaki-it-mx-ospf-if", network_type="p2p", cost_of_interface=100)
    t.eigrp_interface_policy(
        "niwaki-it-mx-eigrp-if",
        interface_controls="split-horizon",
        hello_interval=5,
        hold_interval=15,
    )


def test_protocol_interface_matrix(live_aci: Niwaki) -> None:
    """One L3Out per (protocol x interface-type), each carrying that protocol interface."""
    t = tenant(TN)
    _scaffold(t)
    leaves = _leaves(live_aci)

    for p, proto in enumerate(PROTOCOLS):
        for it, (iftype, (inst, base, encap_base)) in enumerate(IFTYPES.items()):
            seq = p * 3 + it
            vrf = f"niwaki-it-mx-{proto}-{iftype}-vrf"
            name = f"niwaki-it-l3o-mx-{proto}-{iftype}"
            t.vrf(vrf, description=f"VRF for {proto} on {iftype}.")
            out = t.l3out(name, description=f"{proto} over {iftype} interface.")
            out.bind(vrf=vrf).bind(domain=L3DOM)
            if proto == "ospf":
                out.ospf(area_id="0.0.0.1", area_type="regular", description="OSPF area.")
            else:
                out.eigrp(autonomous_system_number=200 + seq, description="EIGRP AS.")

            for lidx, (lname, node_id) in enumerate(leaves, start=1):
                np = out.node_profile(f"np-{lname}")
                np.node_attachment(
                    f"topology/pod-1/node-{node_id}",
                    rtr_id=f"10.13.{seq}.{lidx}",
                    rtr_id_loop_back=False,
                )
                ifp = np.interface_profile(f"if-{lname}")
                # Routed (l3-port) interfaces are exclusive, so each protocol takes
                # its own port; tagged interfaces share a port via distinct encaps.
                port = base + p if inst == "l3-port" else base
                path_kwargs: dict[str, object] = {
                    "if_inst_t": inst,
                    "addr": f"10.13{seq}.{port}.{lidx}/24",
                }
                if encap_base is not None:
                    path_kwargs["encap"] = f"vlan-{encap_base + seq}"
                    path_kwargs["mode"] = "regular"
                ifp.path_attachment(
                    f"topology/pod-1/paths-{node_id}/pathep-[eth1/{port}]",
                    **path_kwargs,  # type: ignore[arg-type]
                )
                if proto == "ospf":
                    ifp.ospf_interface(
                        authentication_type="none", description="OSPF interface."
                    ).bind(ospf_interface_policy="niwaki-it-mx-ospf-if")
                else:
                    ifp.eigrp_interface(description="EIGRP interface.").bind(
                        eigrp_interface_policy="niwaki-it-mx-eigrp-if"
                    )

    t.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — operator only; removes this file's L3Outs and VRFs."""
    for proto in PROTOCOLS:
        for iftype in IFTYPES:
            for dn in (
                f"uni/tn-{TN}/out-niwaki-it-l3o-mx-{proto}-{iftype}",
                f"uni/tn-{TN}/ctx-niwaki-it-mx-{proto}-{iftype}-vrf",
            ):
                with contextlib.suppress(NotFoundError):
                    aci.node(dn).delete()
