"""Day 0 — maintenance (update) groups: even and odd leaves and spines.

Run:
    uv run pytest tests/integration/01_day0/test_007_update_groups.py -m integration -s

To upgrade the fabric without downtime, an operator splits the switches into
maintenance groups by node-ID parity — even and odd — for leaves and spines
separately, then upgrades one group at a time so a redundant pair never loses
both members at once.  Each group selects its switches by node-ID range (one
block per node).
"""

from __future__ import annotations

import pytest

from niwaki import Niwaki
from niwaki.design import fabric

pytestmark = pytest.mark.integration

PLURAL = {"leaf": "leaves", "spine": "spines"}


def test_update_groups(live_aci: Niwaki) -> None:
    # The switches to stagger, grouped by role, from the fabric inventory.
    nodes: dict[str, list[int]] = {"leaf": [], "spine": []}
    for node in live_aci.query("fabricNode").fetch():
        data = node.model_dump(by_alias=True)
        role, node_id = data.get("role"), data.get("id")
        if role in nodes and node_id:
            nodes[role].append(int(node_id))

    fab = fabric()

    # The maintenance policy every group runs (target version + scheduling).
    fab.maintenance_policy("staggered")

    for role in ("leaf", "spine"):
        for kind, parity in (("even", 0), ("odd", 1)):
            members = sorted(node_id for node_id in nodes[role] if node_id % 2 == parity)
            if not members:  # a single spine, say, leaves one parity empty
                continue
            group = fab.maintenance_group(f"{kind}-{PLURAL[role]}")
            group.set(fwtype="switch", selector_type="range")
            group.bind(maintenance_policy="staggered")
            for node_id in members:
                group.node_block(f"node-{node_id}", from_node_id=node_id, to_node_id=node_id)

    fab.push(live_aci)
