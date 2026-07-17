"""External connectivity — OSPF areas and interfaces, exhaustive combinations (non-prod).

Run:
    uv run pytest tests/integration/06_l3out/test_005_ospf.py -m integration -s

OSPF on an L3Out: one L3Out per area type (regular / stub / NSSA, each on a
non-zero area with a representative area-control set), and OSPF interfaces swept
across the authentication types (none / simple / md5) and the interface-policy
network types (point-to-point / broadcast). The OSPF interface rides an SVI on a
VLAN from the shared lane.

Each area type gets its own L3Out (and VRF); addresses use a 10.x scheme. Values
are illustrative. ``wipe(aci)`` is operator-only.
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

# area type -> (area id, area control, auth type). Stub/NSSA cannot be the backbone;
# ospf_interface is a singleton per interface profile, so auth is swept per L3Out.
AREAS = [
    ("regular", "0.0.0.1", "redistribute,summary", "none"),
    ("stub", "0.0.0.2", "redistribute,summary,suppress-fa", "simple"),
    ("nssa", "0.0.0.3", "redistribute,summary", "md5"),
]


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
    t.ospf_interface_policy(
        "niwaki-it-ospf-p2p", network_type="p2p", cost_of_interface=100, hello_interval=10
    )
    t.ospf_interface_policy(
        "niwaki-it-ospf-bcast", network_type="bcast", cost_of_interface=50, hello_interval=30
    )


def test_ospf_areas_and_interfaces(live_aci: Niwaki) -> None:
    """One L3Out per OSPF area type, each with authenticated OSPF interfaces."""
    t = tenant(TN)
    _scaffold(t)
    leaves = _leaves(live_aci)

    for a, (area_type, area_id, area_ctrl, auth) in enumerate(AREAS):
        vrf = f"niwaki-it-ospf-{area_type}-vrf"
        t.vrf(vrf, description=f"VRF for OSPF {area_type}.")
        out = t.l3out(f"niwaki-it-l3o-ospf-{area_type}", description=f"OSPF {area_type} L3Out.")
        out.bind(vrf=vrf).bind(domain=L3DOM)
        out.ospf(
            area_id=area_id,
            area_type=area_type,
            area_cost=10,
            area_ctrl=area_ctrl,
            description=f"OSPF {area_type} area {area_id}.",
        )

        for lidx, (lname, node_id) in enumerate(leaves, start=1):
            np = out.node_profile(f"np-{lname}")
            np.node_attachment(
                f"topology/pod-1/node-{node_id}", rtr_id=f"10.5.{a}.{lidx}", rtr_id_loop_back=False
            )
            ifp = np.interface_profile(f"if-{lname}")
            # Two SVIs share the interface profile's single OSPF interface (a
            # singleton), whose authentication type comes from the area.
            for k in range(2):
                port = 40 + a * 2 + k
                vlan = 2650 + a * 2 + k
                ifp.path_attachment(
                    f"topology/pod-1/paths-{node_id}/pathep-[eth1/{port}]",
                    if_inst_t="ext-svi",
                    addr=f"10.5{a}.{port}.{lidx}/24",
                    encap=f"vlan-{vlan}",
                    mode="regular",
                )
            kwargs: dict[str, object] = {
                "authentication_type": auth,
                "description": f"OSPF interface auth {auth}.",
            }
            if auth != "none":
                # OSPF authentication keys are limited to 8 characters.
                kwargs["authentication_key"] = "ospfk123"
                kwargs["authentication_key_id"] = 1 + a
            ospf_if = ifp.ospf_interface(**kwargs)  # type: ignore[arg-type]
            ospf_if.bind(
                ospf_interface_policy=(
                    "niwaki-it-ospf-p2p" if a % 2 == 0 else "niwaki-it-ospf-bcast"
                )
            )

    t.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — operator only; removes this file's L3Outs and VRFs."""
    for area in AREAS:
        area_type = area[0]
        for dn in (
            f"uni/tn-{TN}/out-niwaki-it-l3o-ospf-{area_type}",
            f"uni/tn-{TN}/ctx-niwaki-it-ospf-{area_type}-vrf",
        ):
            with contextlib.suppress(NotFoundError):
                aci.node(dn).delete()
