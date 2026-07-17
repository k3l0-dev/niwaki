"""External connectivity — static routes and next hops, exhaustive combinations (non-prod).

Run:
    uv run pytest tests/integration/06_l3out/test_007_static_routes.py -m integration -s

Static routes on an L3Out node attachment, swept across the route-control flag
(``bfd`` / ``unspecified``), the aggregated-route boolean with its prefix-length
window, the administrative-preference scale, and both next-hop kinds (a forwarding
``prefix`` next hop and a ``none`` discard next hop). Routes are keyed by prefix
under the node attachment, so many combinations land without any interface.

One VRF backs the L3Out; addresses use a 10.x scheme. Values are illustrative.
``wipe(aci)`` is operator-only.
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
L3OUT = "niwaki-it-l3o-static"
VRF = "niwaki-it-l3o-static-vrf"


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


def test_static_routes(live_aci: Niwaki) -> None:
    """A matrix of static routes and next hops under each leaf node attachment."""
    t = tenant(TN)
    _scaffold(t)
    leaves = _leaves(live_aci)

    t.vrf(VRF, description="VRF for the static-route L3Out.")
    out = (
        t.l3out(
            L3OUT,
            description="Static routes over route-control/aggregated/preference with next hops.",
        )
        .bind(vrf=VRF)
        .bind(domain=L3DOM)
    )

    for lidx, (lname, node_id) in enumerate(leaves, start=1):
        np = out.node_profile(f"np-{lname}", description=f"Node profile for {lname}.")
        att = np.node_attachment(
            f"topology/pod-1/node-{node_id}", rtr_id=f"10.7.0.{lidx}", rtr_id_loop_back=False
        )

        # route-control flag x aggregated (with a valid prefix-length window).
        for i, (rtctrl, agg) in enumerate(
            [("bfd", False), ("unspecified", False), ("bfd", True), ("unspecified", True)]
        ):
            kwargs: dict[str, object] = {
                "aggregated_route": agg,
                "route_controls": rtctrl,
                "preference": 1 + i,
                "description": f"Route rtctrl {rtctrl}, aggregated {agg}.",
            }
            if agg:
                kwargs["start_of_prefix_length"] = 17
                kwargs["end_of_prefix_length"] = 24
            route = att.static_route(f"10.{lidx}0.{i}.0/16", **kwargs)  # type: ignore[arg-type]
            route.next_hop(
                f"10.{lidx}0.{i}.254",
                nexthop_type="prefix",
                preference=1,
                description="Forwarding next hop.",
            )

        # A host route with two forwarding next hops of different preference.
        # COVERAGE GAP: nexthop_type="none" (discard) is rejected with any address
        # ("NextHop of type none should not have a valid prefix"), and the maker
        # requires the address positionally — so the discard next-hop is not
        # exercisable through this path.
        multi = att.static_route("203.0.113.0/24", description="Route with two next hops.")
        multi.next_hop(f"10.79.{lidx}.1", nexthop_type="prefix", preference=1)
        multi.next_hop(f"10.79.{lidx}.2", nexthop_type="prefix", preference=5)

        # Administrative-preference scale.
        for pref in (1, 50, 120, 200, 254):
            r = att.static_route(f"198.51.{lidx}{pref % 100}.0/24", preference=pref)
            r.next_hop(f"10.78.{lidx}.{pref % 254 + 1}", nexthop_type="prefix")

    t.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — operator only; removes this file's L3Out and VRF."""
    for dn in (f"uni/tn-{TN}/out-{L3OUT}", f"uni/tn-{TN}/ctx-{VRF}"):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
