"""Day 0 — fabric bring-up: node registration and BGP settings.

Run:
    uv run pytest tests/integration/01_day0/test_001_init_fabric.py -m integration -s

The first two things an operator does on a brand-new ACI fabric, with the niwaki
SDK: register the switches waiting in the fabric inventory, then set the fabric
BGP ASN and its route reflectors (the spines).
"""

from __future__ import annotations

import pytest

from niwaki import Niwaki
from niwaki.design import controller, fabric

pytestmark = pytest.mark.integration

FABRIC_ASN = 65001


def test_init_fabric(live_aci: Niwaki) -> None:
    # 1 — Register the switches waiting in the fabric inventory.  Leaves get
    #     node IDs from 101, spines from 1001, each named after its role and ID.
    next_id = {"leaf": 101, "spine": 1001}
    spine_ids: list[int] = []
    for node in live_aci.query("dhcpClient").fetch():
        discovered = node.model_dump(by_alias=True)
        serial, role = discovered.get("id"), discovered.get("nodeRole")
        if not serial or role not in next_id:
            continue
        node_id = next_id[role]
        next_id[role] += 1
        controller().fabric_membership().fabric_node_member(
            serial, id=node_id, name=f"{role}-{node_id}", role=role
        ).push(live_aci)
        if role == "spine":
            spine_ids.append(node_id)

    # 2 — Fabric BGP: the ASN and its route reflectors (the spines just registered).
    day0 = fabric()
    bgp = day0.bgp_instance("default")
    bgp.autonomous_system().set(autonomous_system_number=FABRIC_ASN)
    route_reflectors = bgp.route_reflector()
    for spine_id in spine_ids:
        route_reflectors.node(spine_id)

    day0.push(live_aci)
