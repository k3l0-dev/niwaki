"""Day 0 — tenant management: out-of-band and in-band node addresses.

Run:
    uv run pytest tests/integration/01_day0/test_006_tenant_mgmt.py -m integration -s

The operator gives every switch a management IP under the ``mgmt`` tenant:

- **Out-of-band** — a static address per node on the default OOB EPG.
- **In-band** — an in-band BD (on the ``inb`` VRF, with its gateway subnet) and
  an in-band EPG (VLAN encap), then a static address per node.

Addresses are assigned **statically, one relation per node**
(``mgmtRsOoBStNode`` / ``mgmtRsInBStNode``, keyed by the node DN, carrying the
IP and gateway via ``ref()``).  Node DNs come from the live fabric inventory.
"""

from __future__ import annotations

import pytest

from niwaki import Niwaki
from niwaki.design import ref, tenant

pytestmark = pytest.mark.integration

OOB_GATEWAY = "203.0.113.1"
INB_VRF = "inb"
INB_BD = "inb"
INB_VLAN = "vlan-10"
INB_SUBNET = "172.16.100.1/24"  # outside the fabric TEP pool (10.0.0.0/16)
INB_GATEWAY = "172.16.100.1"


def test_tenant_management(live_aci: Niwaki) -> None:
    # The switches to address — leaves and spines from the fabric inventory.
    nodes: dict[int, str] = {}
    for node in live_aci.query("fabricNode").fetch():
        data = node.model_dump(by_alias=True)
        role, node_id = data.get("role"), data.get("id")
        if role in ("leaf", "spine") and node_id:
            nodes[int(node_id)] = f"topology/pod-1/node-{node_id}"

    # Node IDs (101, 1001, …) don't map to host octets, so assign sequentially
    # from .11 — the same host octet for a node's OOB and in-band address.
    host = {node_id: 11 + index for index, (node_id, _) in enumerate(sorted(nodes.items()))}

    mgmt = tenant("mgmt")
    profile = mgmt.management_profile("default")

    # ── Out-of-band ─────────────────────────────────────────────────────────
    oob = profile.out_of_band_epg("default")
    for node_id, node_dn in sorted(nodes.items()):
        oob.bind_dn(
            static_node=ref(node_dn, address=f"203.0.113.{host[node_id]}/24", gateway=OOB_GATEWAY)
        )

    # ── In-band ─────────────────────────────────────────────────────────────
    mgmt.vrf(INB_VRF)  # tn-mgmt's built-in in-band VRF (upsert)
    inb_bd = mgmt.bd(INB_BD).bind(vrf=INB_VRF)
    inb_bd.subnet(INB_SUBNET)  # the in-band gateway

    inb = profile.in_band_epg("inband")
    inb.set(encap=INB_VLAN)
    inb.bind(bd=INB_BD)
    for node_id, node_dn in sorted(nodes.items()):
        inb.bind_dn(
            static_node=ref(node_dn, address=f"172.16.100.{host[node_id]}/24", gateway=INB_GATEWAY)
        )

    mgmt.push(live_aci)
