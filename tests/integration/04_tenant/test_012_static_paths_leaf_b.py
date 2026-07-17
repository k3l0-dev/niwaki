"""Tenant — static path bindings on every interface of the second leaf (non-prod).

Run:
    uv run pytest tests/integration/04_tenant/test_012_static_paths_leaf_b.py -m integration -s

A static-path binding (``fvRsPathAtt``) for **every front-panel interface** of
the second discovered leaf, data-driven from ``l1PhysIf`` at runtime — one per
interface eth1/1 .. eth1/N. Deployment immediacy (immediate / lazy) and tagging
mode (native / regular / untagged) cycle across the ports; encapsulations come
from this domain's VLAN lane (vlan-2500..2599), reused across ports. The first
ports carry the leaf-port children (port security, IGMP/MLD snoop groups, NLB
static group).

Values are illustrative. This file owns tenant ``niwaki-it-sp-b``; ``wipe``
(operator-only) deletes it.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import tenant
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

TN = "niwaki-it-sp-b"
VRF = "niwaki-it-sp-b-vrf"
BD = "niwaki-it-sp-b-bd"
APP = "niwaki-it-sp-b-app"
LEAF_INDEX = 1

MODES = ["regular", "native", "untagged"]
IMMEDIACY = ["immediate", "lazy"]


def _leaf_interfaces(live_aci: Niwaki, leaf_index: int) -> tuple[str | None, list[str]]:
    """Return (leaf node id, sorted front-panel interface ids) for the Nth leaf."""
    leaves = sorted(
        d["id"]
        for node in live_aci.query("fabricNode").fetch()
        if (d := node.model_dump(by_alias=True)).get("role") == "leaf" and d.get("id")
    )
    if leaf_index >= len(leaves):
        return None, []
    leaf = leaves[leaf_index]
    ifaces = [
        d["id"]
        for phys in live_aci.query("l1PhysIf").fetch()
        if (d := phys.model_dump(by_alias=True)).get("id") and f"node-{leaf}/" in d.get("dn", "")
    ]
    ifaces.sort(key=lambda iface: int(iface.rsplit("/", 1)[-1]))
    return leaf, ifaces


def test_static_paths_all_interfaces(live_aci: Niwaki) -> None:
    """A static-path binding per front-panel interface of the second leaf."""
    tn = tenant(TN, description="Static-path bindings on every interface of the second leaf")
    tn.vrf(VRF, description="VRF backing the static-path EPG.")
    tn.bd(BD, description="BD backing the static-path EPG.", unicast_routing=True).bind(vrf=VRF)
    epg = (
        tn.app(APP, description="Application profile for per-interface static paths.")
        .epg("niwaki-it-epg-sp", description="EPG bound onto every leaf interface.")
        .bind(bd=BD)
    )

    leaf, ifaces = _leaf_interfaces(live_aci, LEAF_INDEX)
    if leaf is None or not ifaces:
        pytest.skip("no second leaf / interfaces discovered on the fabric")

    for port, iface in enumerate(ifaces, start=1):
        path = epg.static_path(
            f"topology/pod-1/paths-{leaf}/pathep-[{iface}]",
            descr=f"Static binding on {iface}.",
            encap=f"vlan-{2500 + (port % 100)}",
            deployment_immediacy=IMMEDIACY[port % len(IMMEDIACY)],
            mode=MODES[port % len(MODES)],
        )
        if port <= 3:
            path.port_security(
                f"niwaki-it-portsec-{port}",
                description="Port-security policy on the static path.",
                maximum=100,
                timeout=60,
                port_security_violation="protect",
            )
            path.igmp_snoop_static_group(
                f"225.62.{port}.1", f"10.62.{port}.5", description="IGMP snoop static group."
            )
            path.igmp_snoop_access_group(description="IGMP snoop access group.")
            path.mld_snoop_static_group(
                f"ff05::62:{port}", f"2001:db8:62::{port}", description="MLD snoop static group."
            )
            path.mld_snoop_access_group(description="MLD snoop access group.")
            path.nlb_static_group(
                f"03:bf:0a:3e:{port:02d}:01", description="NLB static group (multicast)."
            )

    tn.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (f"uni/tn-{TN}",):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
