"""External connectivity — L3Out roots, node profiles and leak/label sweep (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/06_l3out/test_001_l3out_base.py -m integration -s

The base of the domain: the ``l3extOut`` roots themselves, swept across every
``enforce_rtctrl`` flag combination, the target-DSCP scale and both MPLS-enabled
states, plus the logical node profiles that hang under them (one per fabric leaf,
discovered at runtime, with router-id loopbacks and both ``rtr_id_loop_back``
states). It also covers the default-route leak policy across its ``always`` /
``criteria`` / ``scope`` matrix, both route-target instrumentation modes, and the
external-connectivity consumer labels.

Each L3Out gets its own VRF so router-ids and loopback addresses never collide
(a loopback IP is unique to one L3Out within a VRF). This file owns the shared
tenant, VLAN lane (2600-2699) and domains; its wipe cascades them. Values are
illustrative. ``wipe(aci)`` is operator-only.
"""

# COVERAGE GAPS (curated but rejected live / out of scope here — reported, not forced):
#   maker:l3extProvLbl@l3extOut   (provider label — infra-tenant L3Outs only; see SR-MPLS file)
#   maker:l3extIntersiteLoopBackIfP@l3extRsNodeL3OutAtt  (multi-site loopback — NDO scope)
#   maker:fvSiteAssociated / mdpClassId / fvOrchsInfo @l3extInstP  (NDO / orchestrator scope)

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import tenant
from niwaki.design._cursor import Cursor
from niwaki.exceptions import NotFoundError
from niwaki.models._generated.aaa.aaaRbacAnnotation import aaaRbacAnnotation
from niwaki.models._generated.tag.tagAnnotation import tagAnnotation
from niwaki.models._generated.tag.tagTag import tagTag

pytestmark = pytest.mark.integration

TN = "niwaki-it-l3out"
POOL = "niwaki-it-l3v"  # VLAN lane 2600-2699
L3DOM = "niwaki-it-l3d"
L2DOM = "niwaki-it-l2d"

# Target-DSCP scale to cycle across the L3Out roots.
DSCP = ["CS0", "CS1", "AF11", "AF21", "AF31", "EF", "VA", "CS7"]
# enforce_rtctrl (L3extCtrlDirection): export route control is always enforced, so
# the valid combinations are export alone or export+import (import-only is rejected).
ENFORCE = ["export", "export,import"]


def _common(cur: Cursor) -> Cursor:
    """Attach the universal children every managed object carries (tag / annotation / RBAC)."""
    cur.mo(tagTag, key="niwaki-it", value="l3out")
    cur.mo(tagAnnotation, key="owner", value="neteng")
    cur.mo(aaaRbacAnnotation, domain="mgmt")
    return cur


def _leaves(aci: Niwaki) -> list[tuple[str, int]]:
    """Discover the fabric's leaf switches at runtime — (name, node-id), sorted by id."""
    found: list[tuple[str, int]] = []
    for node in aci.query("fabricNode").fetch():
        data = node.model_dump(by_alias=True)
        if data.get("role") == "leaf" and data.get("name") and data.get("id"):
            found.append((data["name"], int(data["id"])))
    return sorted(found, key=lambda pair: pair[1])


def scaffold(t: Cursor) -> None:
    """Declare the shared closed-world base: the VLAN lane and the domains."""
    t.infra().vlan_pool(POOL, "static", description="VLAN lane 2600-2699 for L3Out encaps.").range(
        "vlan-2600", "vlan-2699", allocation_mode="static", role="external"
    )
    t.l3_dom(L3DOM).bind(vlan_pool=POOL)
    t.l2_dom(L2DOM).bind(vlan_pool=POOL)


def _mk_l3out(
    t: Cursor,
    name: str,
    seq: int,
    leaves: list[tuple[str, int]],
    *,
    loopback: bool = False,
    **l3out_kwargs: object,
) -> Cursor:
    """Create a VRF + L3Out + a node profile per leaf, with unique router-id/loopback IPs.

    ``seq`` scopes the IP octets so router-ids (``10.<seq>.<leaf>.1``) and loopbacks
    (``10.<seq>.<leaf>.2``) are unique to this L3Out.
    """
    vrf = f"{name}-vrf"
    t.vrf(vrf, description=f"VRF backing {name}.")
    out = t.l3out(name, **l3out_kwargs)  # type: ignore[arg-type]
    out.bind(vrf=vrf).bind(domain=L3DOM)
    for idx, (lname, node_id) in enumerate(leaves, start=1):
        np = out.node_profile(
            f"np-{lname}", description=f"Node profile for {lname}.", dscp_value="AF11"
        )
        att = np.node_attachment(
            f"topology/pod-1/node-{node_id}",
            rtr_id=f"10.{seq}.{idx}.1",
            rtr_id_loop_back=loopback,
            config_issues="none",
        )
        att.loopback(
            loop_back_interface_address=f"10.{seq}.{idx}.2",
            description=f"Secondary loopback on {lname}.",
        )
    return out


def test_l3out_roots(live_aci: Niwaki) -> None:
    """One L3Out per (enforce_rtctrl x MPLS x rtr-id-loopback) combination."""
    t = tenant(
        TN,
        description="Exhaustive L3Out/L2Out coverage: interfaces, BGP/OSPF/EIGRP, route-control.",
    )
    scaffold(t)
    leaves = _leaves(live_aci)

    n = 0
    for enforce in ENFORCE:
        for loopback in (False, True):
            for mpls in (False, True):
                out = _mk_l3out(
                    t,
                    f"niwaki-it-l3o-{enforce.replace(',', '-')}-lb{int(loopback)}-m{int(mpls)}",
                    n + 1,
                    leaves,
                    loopback=loopback,
                    description=f"L3Out enforce {enforce}, lb {loopback}, mpls {mpls}.",
                    enforce_rtctrl=enforce,
                    out_level_dscp=DSCP[n % len(DSCP)],
                    mpls_enabled=mpls,
                )
                if n == 0:
                    _common(out)
                n += 1

    t.push(live_aci)


def test_default_route_leak(live_aci: Niwaki) -> None:
    """Default-route leak policy swept across its always / criteria / scope matrix."""
    t = tenant(TN)
    scaffold(t)
    leaves = _leaves(live_aci)

    combos = [
        ("no", "only", "l3-out"),
        ("yes", "only", "ctx"),
        ("yes", "in-addition", "ctx,l3-out"),
        ("no", "in-addition", "l3-out"),
    ]
    for i, (always, criteria, scope) in enumerate(combos):
        out = _mk_l3out(
            t,
            f"niwaki-it-l3o-leak-{i}",
            20 + i,
            leaves,
            description=f"Leak {always}/{criteria}/{scope}.",
        )
        out.default_route_leak_policy(
            always_advertise_default_leak=always,
            default_leak_advertise_criteria=criteria,
            scope=scope,
        )

    t.push(live_aci)


def test_route_target_and_labels(live_aci: Niwaki) -> None:
    """Route-target instrumentation (both modes) and the consumer labels (both owners)."""
    t = tenant(TN)
    scaffold(t)
    leaves = _leaves(live_aci)

    for i, mode in enumerate(("automatic", "explicit")):
        out = _mk_l3out(
            t, f"niwaki-it-l3o-rt-{mode}", 30 + i, leaves, description=f"Route-target instr {mode}."
        )
        out.route_target_instrumentation_profile(
            route_target_instrumentation_type=mode, description=f"{mode} route targets."
        )

    # Consumer labels (both ownership values) on their own L3Out. A provider label
    # is rejected in a user tenant, so it is covered in the SR-MPLS file instead.
    cons = _mk_l3out(
        t, "niwaki-it-l3o-cons", 35, leaves, description="L3Out carrying consumer labels."
    )
    for owner, color in (("infra", "blue"), ("tenant", "green")):
        cons.consumer_label(
            f"cons-{owner}",
            represents_the_provider_label_ownership=owner,
            tag=color,
            description=f"Consumer label owned by {owner}.",
        )

    t.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — operator only. Owns the tenant + shared domains / VLAN lane."""
    for dn in (
        f"uni/tn-{TN}",
        f"uni/l3dom-{L3DOM}",
        f"uni/l2dom-{L2DOM}",
        f"uni/infra/vlanns-[{POOL}]-static",
    ):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
