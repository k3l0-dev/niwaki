"""Observability — fabric SPAN, combination coverage (non-prod).

Run:
    uv run pytest tests/integration/08_observability/test_006_span_fabric.py -m integration -s

The operator builds fabric SPAN: a source group with a node-span source (span-on-
drop) per fabric node, bridge-domain and VRF sources over several BDs/VRFs in each
direction (the BD/VRF SPAN relations are fabric-only), and a destination group
whose ERSPAN summaries sweep the mode/TTL/DSCP space. A virtual-SPAN session and
destination group round it out.

Exhaustive, non-prod. Nodes are data-driven from the live fabric. ``wipe(aci)``
(operator-only) removes the whole set.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import design
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

# COVERAGE GAPS (curated child in CHILD_MAP, no maker/bind/verb — reported, not forced):
#   spanVSrc / spanVSrcDef carry no target binds (created with attributes only).
#   Universal MO children (tag/annotation/rbac/domain-tag-ref) globally have no maker.
# APIC scope rules honoured: BD/VRF SPAN relations and node span are fabric-only; node span
#   requires span-on-drop; the apic_node destination is rejected live (ERSPAN used instead).

TN = "niwaki-it-obs-span-fab"
SRC = "niwaki-it-fabric-span"
DST = "niwaki-it-fabric-dst"
VSRC = "niwaki-it-fabric-vspan"
VDST = "niwaki-it-fabric-vdest"

VRFS = ("vrf1", "vrf2")
BDS = ("bd1", "bd2", "bd3")
DIRECTIONS = ("both", "in", "out")
ERSPAN_COMBOS = (
    ("visible", 64, 0),
    ("not-visible", "unspecified", 10),
    ("visible", 255, 46),
    ("not-visible", 32, 34),
)


def _node_dns(live_aci: Niwaki) -> list[str]:
    """Return the node DNs of the leaves and spines the fabric reports."""
    dns: list[str] = []
    for node in live_aci.query("fabricNode").fetch():
        data = node.model_dump(by_alias=True)
        if data.get("role") in ("leaf", "spine") and data.get("id"):
            dns.append(f"topology/pod-1/node-{data['id']}")
    return dns or ["topology/pod-1/node-101", "topology/pod-1/node-102"]


def test_fabric_span_sources(live_aci: Niwaki) -> None:
    """Node span (span-on-drop) per node, plus BD and VRF sources per direction."""
    node_dns = _node_dns(live_aci)

    cfg = design()
    fab = cfg.fabric()

    # BDs and VRFs the fabric SPAN sources reference (their relations are fabric-only).
    tenant = cfg.tenant(TN, description="Observability: fabric SPAN BD and VRF targets.")
    for vrf in VRFS:
        tenant.vrf(vrf)
    for i, bd in enumerate(BDS):
        tenant.bd(bd).bind(vrf=VRFS[i % len(VRFS)])

    src = fab.span_source_group(
        SRC, description="Fabric SPAN source group.", administrative_state="enabled"
    )
    src.span_label(DST, description="Match label onto the fabric destination group.", tag="red")

    # Node span requires span-on-drop and is only valid under fabric SPAN.
    for i, node_dn in enumerate(node_dns):
        src.span_source(
            f"node-{i}",
            description=f"Fabric node span, span-on-drop ({node_dn}).",
            span_only_dropped_packets=True,
        ).bind_dn(fabric_node=node_dn)

    for bd in BDS:
        for direction in DIRECTIONS:
            src.span_source(
                f"bd-{bd}-{direction}",
                description=f"Fabric source scoped to BD {bd}, {direction}.",
                direction_ingress_egress_both=direction,
            ).bind(bd=bd)
    for vrf in VRFS:
        for direction in DIRECTIONS:
            src.span_source(
                f"vrf-{vrf}-{direction}",
                description=f"Fabric source scoped to VRF {vrf}, {direction}.",
                direction_ingress_egress_both=direction,
            ).bind(vrf=vrf)

    cfg.push(live_aci)


def test_fabric_span_destinations(live_aci: Niwaki) -> None:
    """One destination group per ERSPAN combo (a group holds a single destination)."""
    cfg = design()
    fab = cfg.fabric()
    for i, (mode, ttl, dscp) in enumerate(ERSPAN_COMBOS):
        grp = fab.span_destination_group(f"{DST}-{i}", description=f"Fabric SPAN dest group {i}.")
        grp.span_destination(f"dst-{i}").vspan_epg_summary(
            description=f"ERSPAN summary: {mode}, ttl {ttl}, dscp {dscp}.",
            destination_ip=f"10.93.{i}.1",
            source_ip_of_erspan_packet=f"10.93.{i}.254",
            flow_id=i + 1,
            mode=mode,
            time_to_live=ttl,
            dscp=dscp,
        )
    cfg.push(live_aci)


def test_fabric_vspan(live_aci: Niwaki) -> None:
    """A virtual-SPAN session and destination group with ERSPAN summaries."""
    cfg = design()
    fab = cfg.fabric()

    session = fab.vspan_session(
        VSRC, description="Fabric virtual-SPAN session.", admin_state="start"
    )
    for direction in DIRECTIONS:
        session.vspan_vsource(f"vsrc-{direction}", direction_ingress_egress_both=direction)

    vdst = fab.vspan_destination_group(VDST, description="Fabric virtual-SPAN destination group.")
    for i, (mode, ttl, dscp) in enumerate(ERSPAN_COMBOS[:3]):
        vdst.vspan_vdestination(f"vdst-{i}").vspan_destination_epg_summary(
            description=f"VSPAN ERSPAN summary: {mode}, ttl {ttl}.",
            destination_ip=f"10.94.{i}.1",
            source_ip_of_erspan_packet=f"10.94.{i}.254",
            flow_id=i + 1,
            mode=mode,
            time_to_live=ttl,
            dscp=dscp,
        )
    cfg.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    dns = [
        f"uni/tn-{TN}",
        f"uni/fabric/srcgrp-{SRC}",
        f"uni/fabric/vsrcgrp-{VSRC}",
        f"uni/fabric/vdestgrp-{VDST}",
    ]
    dns += [f"uni/fabric/destgrp-{DST}-{i}" for i in range(len(ERSPAN_COMBOS))]
    for dn in dns:
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
