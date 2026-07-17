"""External connectivity — route-map bindings at every attachment point (non-prod).

Run:
    uv run pytest tests/integration/06_l3out/test_013_route_map_bindings.py -m integration -s

Route-maps (``rtctrlProfile``) attach at many points on an L3Out, each with a
direction or a source protocol. This file binds route-maps at **every attachment
point the SDK exposes, in both directions**, factored onto separate relations
where import and export are distinct managed objects:

- BGP peer import **and** export route-maps, on a node peer and a loopback peer;
- external-EPG (``l3extInstP``) import **and** export route-control;
- external-EPG subnet import **and** export route-control;
- the L3Out ``default-import`` **and** ``default-export`` route-control profiles;
- the L3Out redistribution route-map for each redistributed source (static /
  direct / attached-host), plus the interleak and dampening route-maps.

Each route-map carries a permit and a deny context over match and action rules, so
the internals are exercised alongside the bindings. One VRF backs the L3Out.
Values are illustrative. ``wipe(aci)`` is operator-only.
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
L3OUT = "niwaki-it-l3o-rmbind"
VRF = "niwaki-it-l3o-rmbind-vrf"

MATCH = "niwaki-it-rmb-match"
ACTION = "niwaki-it-rmb-action"
# attached-host redistribution forbids SetMetric / next-hop-propagation, so it uses
# a lightweight action rule.
ACTION_LIGHT = "niwaki-it-rmb-action-light"
# Reusable route-maps bound at the attachment points below.
ROUTE_MAPS = [
    "niwaki-it-rm-imp",
    "niwaki-it-rm-exp",
    "niwaki-it-rm-redist-static",
    "niwaki-it-rm-redist-direct",
    "niwaki-it-rm-redist-ah",
    "niwaki-it-rm-interleak",
    "niwaki-it-rm-damp",
]


def _leaves(aci: Niwaki) -> list[tuple[str, int]]:
    """Discover the fabric's leaf switches at runtime — (name, node-id), sorted by id."""
    found: list[tuple[str, int]] = []
    for node in aci.query("fabricNode").fetch():
        data = node.model_dump(by_alias=True)
        if data.get("role") == "leaf" and data.get("name") and data.get("id"):
            found.append((data["name"], int(data["id"])))
    return sorted(found, key=lambda pair: pair[1])


def _rules(t: Cursor) -> None:
    """A shared match rule and action rule the route-map contexts reference."""
    mr = t.match_rule(MATCH, description="Match rule for the bound route-maps.")
    mr.match_prefix("10.0.0.0/8", aggregated_route=False, description="Prefix match.")
    mr.match_community("comm").factor("regular:as2-nn2:65000:100", scope="transitive")
    mr.match_as_path("aspath", regular_expression="^65001_")

    ar = t.action_rule_profile(ACTION, description="Action rule for the bound route-maps.")
    ar.set_preference(local_pref=150, description="Local preference.")
    ar.set_metric(metric=100, description="MED.")
    ar.set_community(community="no-export", set_criteria="replace", description="Community.")
    ar.set_weight(weight=200, description="Weight.")

    light = t.action_rule_profile(ACTION_LIGHT, description="Metric-free action rule.")
    light.set_preference(local_pref=120, description="Local preference.")
    light.set_community(community="no-advertise", set_criteria="append", description="Community.")


def _route_map(t: Cursor, name: str, action: str = ACTION, *, with_deny: bool = True) -> None:
    """A reusable route-map with a permit (and optionally a deny) context."""
    setphrase = (
        "set local-pref/community" if action == ACTION_LIGHT else "set pref/metric/community/weight"
    )
    rm = t.route_control_profile(
        name,
        type="combinable",
        description=f"Match prefix/community/AS-path, {setphrase}.",
    )
    permit = rm.route_control_context("permit-10", action="permit", local_order=1)
    permit.bind(match_rule=MATCH)
    permit.route_context_scope().bind(action_rule_profile=action)
    if with_deny:
        rm.route_control_context("deny-20", action="deny", local_order=2).bind(match_rule=MATCH)


def _scaffold(t: Cursor) -> None:
    t.infra().vlan_pool(POOL, "static").range(
        "vlan-2600", "vlan-2699", allocation_mode="static", role="external"
    )
    t.l3_dom(L3DOM).bind(vlan_pool=POOL)
    _rules(t)
    for name in ROUTE_MAPS:
        # Interleak / redistribution / dampening maps must be permit-only (a deny
        # context is rejected); the attached-host map must not carry a metric rule.
        redist_like = any(k in name for k in ("redist", "interleak", "damp"))
        _route_map(
            t,
            name,
            ACTION_LIGHT if name.endswith("redist-ah") else ACTION,
            with_deny=not redist_like,
        )


def test_route_map_bindings(live_aci: Niwaki) -> None:
    """Bind route-maps at every attachment point in both directions."""
    t = tenant(TN)
    _scaffold(t)
    leaves = _leaves(live_aci)

    t.vrf(VRF, description="VRF for the route-map binding L3Out.")
    out = (
        t.l3out(L3OUT, description="Route-maps bound at every attachment point, import and export.")
        .bind(vrf=VRF)
        .bind(domain=L3DOM)
    )
    out.bgp(description="BGP for the peer route-maps.")

    # ── L3Out-level route-maps: interleak, dampening, redistribution per source ──
    out.interleak("niwaki-it-rm-interleak")
    out.dampening("niwaki-it-rm-damp")
    out.redistribute(ref("niwaki-it-rm-redist-static", src="static"))
    out.redistribute(ref("niwaki-it-rm-redist-direct", src="direct"))
    out.redistribute(ref("niwaki-it-rm-redist-ah", src="attached-host"))

    # ── The L3Out's implicit default-import / default-export route-maps ──────────
    di = out.route_control_profile(
        "default-import", type="combinable", description="Default import."
    )
    di.route_control_context("imp-10", action="permit", local_order=1).bind(match_rule=MATCH)
    de = out.route_control_profile(
        "default-export", type="combinable", description="Default export."
    )
    de.route_control_context("exp-10", action="permit", local_order=1).bind(match_rule=MATCH)

    # ── BGP peers: import + export route-maps, on a node peer and a loopback peer ─
    for lidx, (lname, node_id) in enumerate(leaves, start=1):
        np = out.node_profile(f"np-{lname}")
        att = np.node_attachment(
            f"topology/pod-1/node-{node_id}", rtr_id=f"10.14.0.{lidx}", rtr_id_loop_back=False
        )
        node_peer = np.bgp_peer(
            f"172.16.{lidx}.1", description="Node peer with import/export maps."
        )
        node_peer.bind(route_control_profile=ref("niwaki-it-rm-imp", direction="import"))
        node_peer.bind(route_control_profile=ref("niwaki-it-rm-exp", direction="export"))

        loop = att.loopback(f"10.14.1.{lidx}", description="Loopback for the loopback peer.")
        loop_peer = loop.bgp_peer(
            f"172.16.{lidx}.5", description="Loopback peer with import/export."
        )
        loop_peer.bind(route_control_profile=ref("niwaki-it-rm-imp", direction="import"))
        loop_peer.bind(route_control_profile=ref("niwaki-it-rm-exp", direction="export"))

    # ── External EPG: instP + subnet import/export route-control ─────────────────
    epg = out.external_epg("niwaki-it-rmb-epg", description="External EPG with route-control.")
    epg.bind(route_control_profile=ref("niwaki-it-rm-imp", direction="import"))
    epg.bind(route_control_profile=ref("niwaki-it-rm-exp", direction="export"))
    sub = epg.subnet(
        "192.0.2.0/24",
        scope="import-rtctrl,export-rtctrl",
        description="Subnet with import + export route-control.",
    )
    sub.bind(route_control_profile=ref("niwaki-it-rm-imp", direction="import"))
    sub.bind(route_control_profile=ref("niwaki-it-rm-exp", direction="export"))

    t.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — operator only; removes this file's L3Out, VRF and route-maps."""
    dns = [f"uni/tn-{TN}/out-{L3OUT}", f"uni/tn-{TN}/ctx-{VRF}"]
    dns += [f"uni/tn-{TN}/prof-{name}" for name in ROUTE_MAPS]
    dns += [
        f"uni/tn-{TN}/subj-{MATCH}",
        f"uni/tn-{TN}/attr-{ACTION}",
        f"uni/tn-{TN}/attr-{ACTION_LIGHT}",
    ]
    for dn in dns:
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
