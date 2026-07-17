"""Fabric — vPC domain policies and explicit protection groups (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/03_fabric/test_013_vpc.py -m integration -s

Several vPC domain policies covering a spread of peer-dead and delay-restore
timers, and the fabric-wide vPC security policy (a singleton) carrying explicit
protection groups. The protected pairs are **data-driven** from the fabric —
the discovered leaves are paired two at a time, each pair referencing a vPC
domain policy.

Illustrative values — not a real fabric config.

``wipe(aci)`` (operator-only) removes the explicit protection groups and the vPC
domain policies; the singleton security policy is left as-is.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import fabric
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

PREFIX = "niwaki-it-vpcdom"
# (slug, peer dead interval, delay restore timer)
DOMAIN_POLICIES = (
    ("fast", 200, 120),
    ("medium", 400, 240),
    ("slow", 600, 360),
)


def _leaf_pairs(aci: Niwaki) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """Discovered leaves as (node_id, pod_id), grouped into consecutive pairs."""
    leaves: list[tuple[int, int]] = []
    for node in aci.query("fabricNode").fetch():
        data = node.model_dump(by_alias=True)
        if data.get("role") == "leaf" and data.get("id"):
            leaves.append((int(data["id"]), _pod_of(str(data.get("dn", "")))))
    leaves.sort()
    return [(leaves[i], leaves[i + 1]) for i in range(0, len(leaves) - 1, 2)]


def test_vpc_domain_policies(live_aci: Niwaki) -> None:
    fab = fabric()
    for slug, dead, restore in DOMAIN_POLICIES:
        fab.vpc_domain_policy(
            f"{PREFIX}-{slug}",
            description=f"vPC domain {slug}: {dead} ms peer-dead, {restore} s delay-restore.",
            peer_dead_interval=dead,
            delay_restore_tmr=restore,
        )
    fab.push(live_aci)


def test_vpc_pairing_modes(live_aci: Niwaki) -> None:
    # The fabric vPC security policy is a singleton; its pairing type holds one
    # value at a time, so all three modes are factored across successive pushes,
    # ending on ``explicit`` (the mode the explicit protection groups require).
    for pair_type in ("reciprocal", "consecutive", "explicit"):
        fabric().vpc_protection(
            description=f"Fabric vPC security policy, {pair_type} pairing.",
            pair_type=pair_type,
        ).push(live_aci)


def test_vpc_explicit_protection(live_aci: Niwaki) -> None:
    pairs = _leaf_pairs(live_aci)

    fab = fabric()
    # The domain policy the pairs reference is declared in-design.
    fab.vpc_domain_policy(
        f"{PREFIX}-fast",
        description="vPC domain timers for the explicit protection groups.",
        peer_dead_interval=200,
        delay_restore_tmr=120,
    )
    protection = fab.vpc_protection(
        description="Fabric vPC security policy with explicit pairing.",
        pair_type="explicit",
    )
    for idx, (member_a, member_b) in enumerate(pairs, start=1):
        pair = protection.vpc_pair(
            f"niwaki-it-vpc-pair-{idx}",
            logical_pair_id=idx,
        ).bind(vpc_policy=f"{PREFIX}-fast")
        for node_id, pod_id in (member_a, member_b):
            pair.node(
                node_id,
                pod_id=pod_id,
                description=f"vPC member node endpoint for leaf {node_id}.",
            )
    fab.push(live_aci)


def _pod_of(dn: str) -> int:
    """Extract the pod id from a ``topology/pod-N/node-M`` DN (defaults to 1)."""
    for part in dn.split("/"):
        if part.startswith("pod-"):
            with contextlib.suppress(ValueError):
                return int(part.removeprefix("pod-"))
    return 1


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it.

    The vPC security policy singleton (uni/fabric/protpol) is left as-is; its
    explicit protection groups and the vPC domain policies are removed.
    """
    dns = [f"uni/fabric/protpol/expgep-niwaki-it-vpc-pair-{i}" for i in range(1, 9)]
    dns += [f"uni/fabric/vpcInst-{PREFIX}-{slug}" for slug, *_ in DOMAIN_POLICIES]
    for dn in dns:
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
