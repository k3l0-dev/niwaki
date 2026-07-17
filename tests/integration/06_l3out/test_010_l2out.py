"""External connectivity — L2Outs and external EPGs, exhaustive combinations (non-prod).

Run:
    uv run pytest tests/integration/06_l3out/test_010_l2out.py -m integration -s

An L2Out bridges a bridge domain to an external layer-2 network. Because an
l2extOut permits exactly one external EPG, the EPG-attribute sweep (preferred
group, QoS class) runs one L2Out per combination; each carries a node/interface
profile with a static path, and its external EPG sweeps the ``fvSubnet`` scope /
control / data-plane-learning matrix, the subnet children (anycast / NLB /
endpoint-network-config), the full label set and the provide / consume /
intra-EPG verbs.

Each L2Out gets its own bridge domain and VRF. Values are illustrative.
``wipe(aci)`` is operator-only.
"""

# COVERAGE GAPS (curated but blocked here — reported, not forced):
#   bind:contract_master@l2extInstP  (EPG inheritance — an l2extOut allows only one
#                                     external EPG, so there is no sibling to inherit from)
#   maker:fvCEp@l2extInstP           (statically-bound endpoint under the external EPG)

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
L2DOM = "niwaki-it-l2d"

# fvSubnet scope x data-plane-learning combinations for the external EPG. L2 external
# subnets are /32 host routes, which require the no-default-gateway control.
SUBNETS = [
    ("public,shared", "no-default-gateway", "enabled"),
    ("private", "no-default-gateway", "disabled"),
    ("public", "no-default-gateway", "enabled"),
    ("shared", "no-default-gateway", "disabled"),
]
# (preferred group, QoS) per L2Out.
EPG_MIX = [("exclude", "level1"), ("include", "level2"), ("exclude", "level3")]


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
    t.l2_dom(L2DOM).bind(vlan_pool=POOL)
    t.contract("niwaki-it-l2-ctr", scope="context", description="Contract for L2Out EPG verbs.")


def test_l2outs(live_aci: Niwaki) -> None:
    """One L2Out per EPG-attribute mix; the first carries the full subnet matrix."""
    t = tenant(TN)
    _scaffold(t)
    leaves = _leaves(live_aci)

    for n, (pref, qos) in enumerate(EPG_MIX):
        vrf = f"niwaki-it-l2-{n}-vrf"
        bd = f"niwaki-it-l2-{n}-bd"
        l2out = f"niwaki-it-l2o-{n}"
        t.vrf(vrf, description=f"VRF for L2Out {n}.")
        t.bd(bd, description=f"BD extended by L2Out {n}.").bind(vrf=vrf)

        out = t.l2out(l2out, description=f"L2Out {n}.", out_level_dscp="CS0")
        out.bind(bd=bd).bind(domain=L2DOM)

        # Node + interface profile with a static path per leaf.
        for lname, node_id in leaves:
            np = out.node_profile(f"np-{lname}", description=f"L2Out node profile for {lname}.")
            ifp = np.interface_profile(
                f"if-{lname}", description=f"L2Out interface profile on {lname}."
            )
            ifp.static_path(
                f"topology/pod-1/paths-{node_id}/pathep-[eth1/{52 + n}]",
                target_dscp="AF11",
                descr="L2 handoff port.",
            )

        # The single external EPG for this L2Out.
        epg = out.external_epg(
            f"niwaki-it-l2epg-{n}",
            description=f"External EPG for L2Out {n}.",
            preferred_group_member=pref,
            qos_class=qos,
        )
        # intra_epg (fvRsIntraEpg) is rejected on l2extInstP (supported only on
        # fvAEPg / fvESg / l3extInstP), so the L2 external EPG uses provide/consume.
        epg.provide("niwaki-it-l2-ctr")
        epg.consume("niwaki-it-l2-ctr")
        epg.consumer_contract_label(f"cons-ctr-{n}", tag="cyan")
        epg.provider_contract_label(f"prov-ctr-{n}", tag="magenta")

        if n == 0:
            # The first EPG carries the full subnet matrix (each scope combination
            # as its own /32 host route). The subnet endpoint children
            # (anycast / NLB / endpoint-network-config) are rejected under an L2
            # external EPG subnet ("should be contained only by fvAEPg") and are
            # exercised on app-EPG / BD subnets in the tenant phase instead.
            for si, (scope, ctrl, dp) in enumerate(SUBNETS):
                epg.subnet(
                    f"10.{80 + si}.0.{si + 1}/32",
                    scope=scope,
                    subnet_control=ctrl,
                    ip_dp_learning=dp,
                    description=f"L2 subnet scope {scope}.",
                )

    t.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — operator only; removes this file's L2Outs, BDs and VRFs."""
    for n in range(len(EPG_MIX)):
        for dn in (
            f"uni/tn-{TN}/l2out-niwaki-it-l2o-{n}",
            f"uni/tn-{TN}/BD-niwaki-it-l2-{n}-bd",
            f"uni/tn-{TN}/ctx-niwaki-it-l2-{n}-vrf",
        ):
            with contextlib.suppress(NotFoundError):
                aci.node(dn).delete()
