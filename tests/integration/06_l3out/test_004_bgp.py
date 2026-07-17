"""External connectivity — BGP peers, exhaustive control combinations (non-prod).

Run:
    uv run pytest tests/integration/06_l3out/test_004_bgp.py -m integration -s

BGP peering on an L3Out, swept broadly: one peer per local-AS propagation mode
(each with an autonomous-system profile), one per neighbour max-prefix action
(each bound to its own prefix policy and a route-control profile), one per
private-AS control, a fully-loaded peer exercising the address-family / peer /
BFD control flags at once, and a peer carrying a site-of-origin. The node's BGP
protocol profile carries best-path and timers policies. Peers are keyed by
address (unique per node per VRF); no interface is required for the config to
land.

One VRF per BGP-enabled L3Out. Addresses use a 10.x / 172.16.x scheme. Values are
illustrative. ``wipe(aci)`` is operator-only.
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
L3OUT = "niwaki-it-l3o-bgp"
VRF = "niwaki-it-l3o-bgp-vrf"

ASN_PROP = ["dual-as", "no-prepend", "none", "replace-as"]
MAX_PFX_ACTION = ["log", "reject", "restart", "shut"]
# Private-AS control: the three flags are interdependent and the APIC accepts them
# only all together (replace-as alone, or remove-all without remove-exclusive, are
# both rejected). So the one valid non-empty combination is all three.
PRIVATE_AS = ["remove-all,remove-exclusive,replace-as"]


def _leaves(aci: Niwaki) -> list[tuple[str, int]]:
    """Discover the fabric's leaf switches at runtime — (name, node-id), sorted by id."""
    found: list[tuple[str, int]] = []
    for node in aci.query("fabricNode").fetch():
        data = node.model_dump(by_alias=True)
        if data.get("role") == "leaf" and data.get("name") and data.get("id"):
            found.append((data["name"], int(data["id"])))
    return sorted(found, key=lambda pair: pair[1])


def _scaffold(t: Cursor) -> None:
    """VLAN lane, L3 domain, the prefix / route-control / best-path / timers policies."""
    t.infra().vlan_pool(POOL, "static").range(
        "vlan-2600", "vlan-2699", allocation_mode="static", role="external"
    )
    t.l3_dom(L3DOM).bind(vlan_pool=POOL)
    t.route_control_profile("niwaki-it-bgp-rc", type="global", description="Peer route-control.")
    for act in MAX_PFX_ACTION:
        t.bgp_peer_prefix_policy(
            f"niwaki-it-pfx-{act}",
            max_prefix_action=act,
            max_number_of_prefixes=10000,
            warning_threshold=75,
            description=f"Prefix policy, action {act}.",
        )
    t.bgp_best_path_control_policy(
        "niwaki-it-bestpath", best_path_control="asPathMultipathRelax", description="Best path."
    )
    # Timers policies: one with the graceful-restart helper control, one without
    # (graceful_restart_controls only accepts the single flag 'helper').
    t.bgp_timers_policy(
        "niwaki-it-timers-0",
        hold_interval=180,
        keepalive_interval=60,
        max_as_limit=1,
        graceful_restart_controls="helper",
        description="Timers with graceful-restart helper.",
    )
    t.bgp_timers_policy(
        "niwaki-it-timers-1",
        hold_interval=90,
        keepalive_interval=30,
        max_as_limit=2,
        description="Timers without graceful restart.",
    )


def test_bgp_peers(live_aci: Niwaki) -> None:
    """A broad matrix of node-level BGP peers on a BGP-enabled L3Out."""
    t = tenant(TN)
    _scaffold(t)
    leaves = _leaves(live_aci)

    t.vrf(VRF, description="VRF for the BGP L3Out.")
    out = (
        t.l3out(
            L3OUT, description="BGP peers over AF/control/private-AS/local-AS and site-of-origin."
        )
        .bind(vrf=VRF)
        .bind(domain=L3DOM)
    )
    out.bgp(description="BGP enabled on this L3Out.")

    for lidx, (lname, node_id) in enumerate(leaves, start=1):
        np = out.node_profile(f"np-{lname}", description=f"Node profile for {lname}.")
        np.node_attachment(
            f"topology/pod-1/node-{node_id}", rtr_id=f"10.4.0.{lidx}", rtr_id_loop_back=False
        )

        # Local-AS propagation modes, each with an autonomous-system profile.
        for i, prop in enumerate(ASN_PROP):
            peer = np.bgp_peer(
                f"172.16.{lidx}.{10 + i}",
                administrative_state="enabled",
                description=f"Peer local-as {prop}.",
            )
            peer.autonomous_system_profile(
                autonomous_system_number=65000 + i, description="Remote AS."
            )
            peer.local_autonomous_system_profile(local_asn=65100 + i, asn_propagation=prop)

        # Neighbour max-prefix actions, each bound to its prefix + route-control policy.
        for j, act in enumerate(MAX_PFX_ACTION):
            peer = np.bgp_peer(
                f"172.16.{lidx}.{20 + j}",
                administrative_state="enabled",
                description=f"Peer max-prefix {act}.",
            )
            peer.bind(
                bgp_peer_prefix_policy=f"niwaki-it-pfx-{act}",
                route_control_profile="niwaki-it-bgp-rc",
            )

        # Private-AS controls.
        for k, pas in enumerate(PRIVATE_AS):
            np.bgp_peer(
                f"172.16.{lidx}.{30 + k}",
                private_as_control=pas,
                description=f"Peer private-as {pas}.",
            )

        # A fully-loaded peer: every control family at once, admin down.
        loaded = np.bgp_peer(
            f"172.16.{lidx}.40",
            administrative_state="disabled",
            address_type_af_controls="af-ucast,af-mcast",
            peer_af_controls="send-com,send-ext-com",
            peer_af_controls_ext="send-domain-path",
            peer_controls="bfd,dis-conn-check",
            allowed_self_as_count=3,
            ebgp_multihop_ttl_value=5,
            weight=100,
            password="niwaki-bgp-secret",
            asn_name="upstream",
            description="Fully-loaded peer, admin down.",
        )
        loaded.local_autonomous_system_profile(local_asn=65200, asn_propagation="replace-as")

        # A peer carrying a site-of-origin (extended community form).
        soo = np.bgp_peer(f"172.16.{lidx}.50", description="Peer with site-of-origin.")
        soo.site_of_origin_profile(site_of_origin="extended:as2-nn2:65001:100", description="SOO.")

        # The node's BGP protocol profile: best-path + timers.
        np.protocol_profile(name="default").bind(
            bgp_best_path_control_policy="niwaki-it-bestpath",
            bgp_timers_policy="niwaki-it-timers-0",
        )

    t.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — operator only; removes this file's L3Out and VRF."""
    for dn in (f"uni/tn-{TN}/out-{L3OUT}", f"uni/tn-{TN}/ctx-{VRF}"):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
