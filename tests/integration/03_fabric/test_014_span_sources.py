"""Fabric — SPAN source groups (exhaustive combinations, non-prod).

Run:
    uv run pytest tests/integration/03_fabric/test_014_span_sources.py -m integration -s

One SPAN source group per administrative state, each carrying a SPAN source per
mirror direction (ingress / egress / both) tied to a fabric-discovered access
port, a VSPAN source and abstract VSPAN source definition per direction, and a
single match label (a session references exactly one destination). A second set
of minimal source groups sweeps a range of match-label colors.

Exhaustive combination coverage, illustrative values — not a real fabric config.
SPAN sources are tied to fabric-discovered ports (``bind_dn`` to real
``topology/...`` path DNs).

``wipe(aci)`` (operator-only) removes every SPAN source group.

# COVERAGE GAPS / constraints:
#   - A SPAN source group session accepts only one match label (one
#     destination), so label colors are swept across separate groups.
#   - Node-level SPAN (span_source fabric_node bind with span-on-drop) is not
#     exercised: the APIC allows only one span-on-drop session per node.
#   - span_source binds epg / bd / vrf / l3out / filter_group target tenant and
#     infra objects (cross-domain) and are exercised in those phases.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import fabric
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

PREFIX = "niwaki-it-span-src"
COLOR_PREFIX = "niwaki-it-span-src-color"
ADMIN_STATES = ("enabled", "disabled")
DIRECTIONS = ("in", "out", "both")
# One label color per admin state, and the extra colors swept below.
ADMIN_COLORS = {"enabled": "red", "disabled": "blue"}
EXTRA_COLORS = ("green", "gold", "cyan", "magenta")


def test_span_source_groups(live_aci: Niwaki) -> None:
    leaf = _first_leaf(live_aci)

    fab = fabric()
    for admin in ADMIN_STATES:
        group = fab.span_source_group(
            f"{PREFIX}-{admin}",
            description=f"SPAN source direction sweep (admin {admin}).",
            administrative_state=admin,
        )
        for port, direction in enumerate(DIRECTIONS, start=10):
            group.span_source(
                f"src-{direction}",
                description=f"Port SPAN source, {direction}.",
                direction_ingress_egress_both=direction,
                span_only_dropped_packets=False,
            ).bind_dn(path=_path_dn(leaf, f"eth1/{port}"))
            group.vspan_source(
                f"vsrc-{direction}",
                description=f"VSPAN source, {direction}.",
                direction_ingress_egress_both=direction,
            )
            group.vspan_source_def(
                f"vsrcdef-{direction}",
                description=f"Abstract VSPAN source definition, {direction}.",
                direction_ingress_egress_both=direction,
            )
        # A source-group session references exactly one destination (one label).
        group.span_label(
            f"match-{ADMIN_COLORS[admin]}",
            description=f"Match label, {ADMIN_COLORS[admin]}.",
            tag=ADMIN_COLORS[admin],
        )
    fab.push(live_aci)


def test_span_label_colors(live_aci: Niwaki) -> None:
    leaf = _first_leaf(live_aci)

    fab = fabric()
    for idx, color in enumerate(EXTRA_COLORS):
        group = fab.span_source_group(
            f"{COLOR_PREFIX}-{color}",
            description=f"SPAN match-label color sweep ({color}).",
            administrative_state="enabled",
        )
        group.span_source(
            "src",
            description=f"Port SPAN source for the {color} label.",
            direction_ingress_egress_both="both",
            span_only_dropped_packets=False,
        ).bind_dn(path=_path_dn(leaf, f"eth1/{20 + idx}"))
        group.span_label(
            f"match-{color}",
            description=f"Match label, {color}.",
            tag=color,
        )
    fab.push(live_aci)


def _first_leaf(aci: Niwaki) -> str:
    """DN of the lowest-numbered leaf discovered in the fabric."""
    found: list[tuple[int, str]] = []
    for node in aci.query("fabricNode").fetch():
        data = node.model_dump(by_alias=True)
        if data.get("role") == "leaf" and data.get("id") and data.get("dn"):
            found.append((int(data["id"]), str(data["dn"])))
    return sorted(found)[0][1]


def _path_dn(node_dn: str, interface: str) -> str:
    """Build a ``fabricPathEp`` DN for ``interface`` on the switch at ``node_dn``."""
    node_id = node_dn.rsplit("/node-", 1)[1]
    prefix = node_dn.rsplit("/node-", 1)[0]
    return f"{prefix}/paths-{node_id}/pathep-[{interface}]"


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    dns = [f"uni/fabric/srcgrp-{PREFIX}-{a}" for a in ADMIN_STATES]
    dns += [f"uni/fabric/srcgrp-{COLOR_PREFIX}-{c}" for c in EXTRA_COLORS]
    for dn in dns:
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
